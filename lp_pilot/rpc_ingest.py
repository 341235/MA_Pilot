"""Validate archived RPC inputs against their raw responses and checksums.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .data import (DataContractError, POOL, normalize_event, order_events,
                   parse_integer, read_rows, sha256_file, utc_seconds)


def checked_artifact(record: dict, root: str | Path, *, path_key: str = "evidence_path",
                     sha_key: str = "evidence_sha256") -> dict:
    """Read an indexed artifact and verify its checksum."""
    value, digest = record.get(path_key), record.get(sha_key)
    if not isinstance(value, str) or not value or not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
        raise DataContractError("RPC evidence requires an artifact path and SHA256")
    path = Path(value)
    path = path if path.is_absolute() else Path(root) / path
    if not path.is_file() or sha256_file(path) != digest.lower():
        raise DataContractError("RPC response artifact absent or checksum mismatches")
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise DataContractError("RPC response artifact must be an object")
    return payload


def abi_words(value: Any, count: int) -> list[int]:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", value) or len(value) != 2 + 64 * count:
        raise DataContractError("RPC ABI result shape mismatch")
    return [int(value[i:i + 64], 16) for i in range(2, len(value), 64)]


def signed_word(value: int, bits: int) -> int:
    result = value - (1 << 256) if value >= 1 << 255 else value
    if not -(1 << (bits - 1)) <= result < 1 << (bits - 1):
        raise DataContractError("RPC ABI signed integer width mismatch")
    return result


def rpc_call_value(record: dict, *, method: str, params: list) -> Any:
    """Extract a result from the saved RpcClient response."""
    if record.get("method") != method or record.get("params") != params or "result" not in record:
        raise DataContractError("RPC evidence method, parameters or result mismatch")
    return record["result"]


def historical_call(evidence: dict, name: str, *, pool_address: str = POOL,
                    argument: int | None = None, count: int = 1) -> list[int]:
    from .rpc import SELECTORS
    identifier = evidence.get("block_identifier")
    if identifier != {"blockHash": evidence.get("block_hash"), "requireCanonical": True}:
        raise DataContractError("Historical evidence requires a canonical block hash identifier")
    selector = SELECTORS[name]
    record = evidence["calls"][name]
    if argument is not None:
        selector += format(argument % (1 << 256), "064x")
        record = record[str(argument)]
    value = rpc_call_value(record, method="eth_call", params=[{
        "to": pool_address.lower(), "data": selector}, identifier])
    return abi_words(value, count)


def historical_state(evidence: dict, *, pool_address: str = POOL) -> dict:
    """Decode state from hash-pinned RPC results, excluding index declarations."""
    if evidence.get("source_kind") != "historical_rpc" or parse_integer(evidence.get("chain_id")) != 1 or str(evidence.get("pool_address")).lower() != pool_address.lower():
        raise DataContractError("Historical evidence source, chain or pool mismatch")
    block = parse_integer(evidence["block_number"])
    block_hash = evidence["block_hash"]
    if block < 0 or not isinstance(block_hash, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", block_hash):
        raise DataContractError("Historical evidence block identity invalid")
    before, after = evidence.get("block_header_before"), evidence.get("block_header_after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise DataContractError("Historical evidence requires before/after block headers")
    for header in (before, after):
        if parse_integer(header["number"]) != block or str(header["hash"]).lower() != block_hash.lower():
            raise DataContractError("Historical response/header block identity mismatch")
    if parse_integer(before["timestamp"]) != parse_integer(after["timestamp"]):
        raise DataContractError("Historical before/after header timestamp mismatch")
    slot = historical_call(evidence, "slot0", pool_address=pool_address, count=7)
    return {"block_number": block, "block_hash": block_hash.lower(),
            "timestamp": parse_integer(before["timestamp"]), "sqrt_price_x96": slot[0],
            "tick": signed_word(slot[1], 24),
            "liquidity": historical_call(evidence, "liquidity", pool_address=pool_address)[0],
            "fee_protocol0": slot[5] & 15, "fee_protocol1": slot[5] >> 4,
            "fee_growth_global0_x128": historical_call(evidence, "feeGrowthGlobal0X128", pool_address=pool_address)[0],
            "fee_growth_global1_x128": historical_call(evidence, "feeGrowthGlobal1X128", pool_address=pool_address)[0]}


def validate_rpc_snapshot(snapshot: dict, snapshot_path: str | Path, *, root: str | Path,
                          pool_address: str = POOL, expected_block: int,
                          tick_spacing: int = 10, fee_pips: int = 500) -> dict:
    """Validate every bitmap word and initialized tick against saved RPC data."""
    from .replay import PoolState
    from .uniswap import MIN_TICK, MAX_TICK
    path = Path(snapshot_path)
    sidecar = path.with_name(path.name + ".manifest.json")
    if not sidecar.exists():
        raise DataContractError("Complete RPC snapshot manifest is missing")
    manifest = json.loads(sidecar.read_text())
    if manifest.get("source") != "ethereum_rpc_snapshot" or manifest.get("complete") is not True:
        raise DataContractError("RPC snapshot manifest source/completeness invalid")
    if manifest.get("snapshot_sha256") != sha256_file(path):
        raise DataContractError("RPC snapshot checksum mismatch")
    if snapshot.get("ticks_complete") is not True or not snapshot.get("provenance") or "synthetic" in str(snapshot["provenance"]).lower():
        raise DataContractError("Complete historical snapshot provenance is required")
    if parse_integer(snapshot["block_number"]) != expected_block:
        raise DataContractError("RPC snapshot must be at first processed block minus one")
    evidence = checked_artifact(manifest, root)
    actual = historical_state(evidence, pool_address=pool_address)
    for key, value in actual.items():
        observed = str(snapshot.get(key)).lower() if key == "block_hash" else parse_integer(snapshot.get(key))
        if observed != value:
            raise DataContractError(f"Snapshot differs from archived RPC response: {key}")
    for name, expected in (("tickSpacing", tick_spacing), ("fee", fee_pips)):
        if historical_call(evidence, name, pool_address=pool_address)[0] != expected:
            raise DataContractError("RPC snapshot pool immutable mismatch")
    if parse_integer(snapshot["tick_spacing"]) != tick_spacing or parse_integer(snapshot["fee_pips"]) != fee_pips:
        raise DataContractError("RPC snapshot configured pool immutable mismatch")
    first, last = (MIN_TICK // tick_spacing) >> 8, (MAX_TICK // tick_spacing) >> 8
    expected_words = set(range(first, last + 1))
    bitmap = snapshot.get("bitmap_words", {})
    if {parse_integer(word) for word in bitmap} != expected_words or {parse_integer(word) for word in evidence["calls"].get("tickBitmap", {})} != expected_words:
        raise DataContractError("RPC snapshot does not contain all bitmap words")
    if (manifest.get("bitmap_word_min"), manifest.get("bitmap_word_max"), manifest.get("bitmap_words_count")) != (first, last, len(expected_words)):
        raise DataContractError("RPC snapshot manifest bitmap coverage mismatch")
    initialized = set()
    for word in sorted(expected_words):
        value = historical_call(evidence, "tickBitmap", pool_address=pool_address, argument=word)[0]
        if parse_integer(bitmap[str(word)]) != value:
            raise DataContractError("RPC snapshot bitmap differs from archived RPC response")
        for bit in range(256):
            if value & (1 << bit):
                tick = (word * 256 + bit) * tick_spacing
                if not MIN_TICK <= tick <= MAX_TICK:
                    raise DataContractError("RPC snapshot bitmap initializes an out-of-range tick")
                initialized.add(tick)
    ticks = snapshot.get("ticks", {})
    if not isinstance(ticks, dict) or {parse_integer(tick) for tick in ticks} != initialized or {parse_integer(tick) for tick in evidence["calls"].get("ticks", {})} != initialized:
        raise DataContractError("RPC snapshot tick set differs from full bitmap")
    if manifest.get("initialized_tick_count") != len(initialized):
        raise DataContractError("RPC snapshot manifest initialized tick count mismatch")
    for tick in sorted(initialized):
        values = historical_call(evidence, "ticks", pool_address=pool_address, argument=tick, count=8)
        row = ticks[str(tick)]
        decoded = {"liquidity_gross": values[0], "liquidity_net": signed_word(values[1], 128),
                   "fee_growth_outside0_x128": values[2], "fee_growth_outside1_x128": values[3]}
        if values[7] != 1 or row.get("initialized") is not True:
            raise DataContractError("RPC snapshot tick was not initialized in historical call")
        if any(parse_integer(row.get(key)) != value for key, value in decoded.items()):
            raise DataContractError("RPC snapshot tick differs from archived RPC response")
    PoolState.from_snapshot(snapshot)
    return {**manifest, "validated_bitmap_words": len(expected_words), "validated_initialized_ticks": len(initialized),
            "provenance_qualifier": "Full tick state checked against hash-pinned historical calls from the same RPC provider; provider authenticity is not independently attested."}


def _load_rpc_export(path: str | Path, manifest_path: str | Path, *, pool_address: str = POOL,
                     start: Any = None, end: Any = None, root: str | Path | None = None
                     ) -> tuple[list[dict], dict]:
    """Reconcile the event table with every archived bounded log response."""
    path, manifest_path = Path(path), Path(manifest_path)
    root = manifest_path.parent if root is None else Path(root)
    manifest = json.loads(manifest_path.read_text())
    required = {"source", "chain_id", "pool_address", "source_row_count", "normalized_row_count",
                "coverage_start", "coverage_end", "block_start", "block_end_exclusive", "sha256",
                "chunks", "boundary_headers", "block_headers", "coverage_complete", "retrieved_at"}
    if not isinstance(manifest, dict) or required - manifest.keys():
        raise DataContractError("RPC export manifest missing required fields")
    if manifest["source"] != "ethereum_rpc" or parse_integer(manifest["chain_id"]) != 1 or str(manifest["pool_address"]).lower() != pool_address.lower():
        raise DataContractError("RPC export source, chain or pool mismatch")
    if manifest["coverage_complete"] is not True or manifest.get("duplicates_removed", 0) != 0:
        raise DataContractError("RPC export is incomplete or removed duplicates")
    if not isinstance(manifest["sha256"], str) or sha256_file(path) != manifest["sha256"].lower():
        raise DataContractError("RPC export checksum mismatch")
    coverage_start, coverage_end = utc_seconds(manifest["coverage_start"]), utc_seconds(manifest["coverage_end"])
    requested_start = coverage_start if start is None else utc_seconds(start)
    requested_end = coverage_end if end is None else utc_seconds(end)
    if not coverage_start <= requested_start < requested_end <= coverage_end:
        raise DataContractError("RPC export does not cover the full requested interval")
    block_start, block_end = parse_integer(manifest["block_start"]), parse_integer(manifest["block_end_exclusive"])
    if block_start < 0 or block_end < block_start:
        raise DataContractError("Invalid RPC export block interval")

    def header_map(rows: list) -> dict[int, dict]:
        result = {}
        for row in rows:
            number = parse_integer(row["block_number"])
            if number in result or not re.fullmatch(r"0x[0-9a-fA-F]{64}", str(row["block_hash"])):
                raise DataContractError("Duplicate or invalid RPC block header")
            result[number] = {"block_number": number, "block_hash": row["block_hash"].lower(),
                              "timestamp": parse_integer(row["timestamp"])}
        return result

    boundaries, headers = header_map(manifest["boundary_headers"]), header_map(manifest["block_headers"])
    for number, stamp in ((block_start, coverage_start), (block_end, coverage_end)):
        if number not in boundaries or boundaries[number]["timestamp"] < stamp:
            raise DataContractError("RPC timestamp boundary header missing or incorrect")
        if number and (number - 1 not in boundaries or boundaries[number - 1]["timestamp"] >= stamp):
            raise DataContractError("RPC timestamp boundary excludes an earlier qualifying block")
    for number, header in boundaries.items():
        if headers.get(number) != header:
            raise DataContractError("RPC boundary header differs from saved block headers")
    rows = read_rows(path)
    if len(rows) != parse_integer(manifest["source_row_count"]) or len(rows) != parse_integer(manifest["normalized_row_count"]):
        raise DataContractError("RPC export row count differs from acquisition counts")
    events = order_events(normalize_event(row, pool_address) for row in rows)
    for event in events:
        header = headers.get(event["block_number"])
        if not block_start <= event["block_number"] < block_end or not coverage_start <= event["timestamp"] < coverage_end:
            raise DataContractError("RPC event outside declared coverage")
        if header is None or (event["block_hash"], event["timestamp"]) != (header["block_hash"], header["timestamp"]):
            raise DataContractError("RPC event block hash or timestamp differs from header")
    cursor, reconciled = block_start, 0
    for chunk in manifest["chunks"]:
        first, last = parse_integer(chunk["from_block"]), parse_integer(chunk["to_block"])
        if first != cursor or not first <= last < block_end or last - first >= 10:
            raise DataContractError("RPC chunks contain a gap, overlap or invalid interval")
        cursor = last + 1
        selected = [row for row in events if first <= row["block_number"] <= last]
        if parse_integer(chunk["row_count"]) != len(selected):
            raise DataContractError("RPC chunk row count differs from event table")
        artifact = checked_artifact(chunk, root)
        logs = rpc_call_value(artifact, method="eth_getLogs", params=[{
            "address": pool_address.lower(), "fromBlock": hex(first), "toBlock": hex(last)}])
        if not isinstance(logs, list) or len(logs) != len(selected):
            raise DataContractError("RPC raw log count differs from event table")
        import hashlib
        digest = hashlib.sha256(json.dumps(logs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if digest != chunk.get("raw_logs_sha256"):
            raise DataContractError("RPC raw log checksum mismatch")
        decoded = []
        for raw in logs:
            number = parse_integer(raw["blockNumber"])
            if number not in headers or not first <= number <= last:
                raise DataContractError("RPC raw log is outside its requested chunk")
            decoded.append(normalize_event({**raw, "timestamp": headers[number]["timestamp"]}, pool_address))
        for raw, row in zip(order_events(decoded), selected):
            for key, value in raw.items():
                observed = row.get(key)
                if key == "topics" and isinstance(observed, str):
                    observed = json.loads(observed)
                if observed != value:
                    raise DataContractError(f"RPC event differs from archived raw log: {key}")
        reconciled += len(selected)
    if cursor != block_end or reconciled != len(events):
        raise DataContractError("RPC chunks do not cover the full declared block interval")
    selected = [event for event in events if requested_start <= event["timestamp"] < requested_end]
    return selected, {**manifest, "validated_row_count": len(events), "selected_row_count": len(selected),
                      "raw_response_reconciliation": "all bounded log artifacts decoded and reconciled",
                      "completeness_caveat": "Contiguous historical block queries, archived raw responses and headers use one RPC provider; independent provider completeness is not established."}


def load_rpc_export(path: str | Path, manifest_path: str | Path, *, pool_address: str = POOL,
                    start: Any = None, end: Any = None, root: str | Path | None = None
                    ) -> tuple[list[dict], dict]:
    """Wrap malformed evidence in a DataContractError."""
    try:
        return _load_rpc_export(path, manifest_path, pool_address=pool_address, start=start, end=end, root=root)
    except (KeyError, TypeError, AttributeError) as exc:
        raise DataContractError(f"Malformed RPC export or response artifact ({type(exc).__name__})") from None

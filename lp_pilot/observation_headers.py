"""Prove pool observation times using archived block headers and complete logs."""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .acquire import make_rpc_client
from .config import data_path, decisions
from .data import DataContractError, parse_integer, sha256_file
from .rpc import RpcError
from .rpc_ingest import checked_artifact, load_rpc_export, rpc_call_value
from .storage import write_json
from .temporal import benchmark_grid


def _path(root: Path, value: str | Path) -> Path:
    value = Path(value)
    return value if value.is_absolute() else root / value


def _relative(root: Path, value: Path) -> str:
    value, root = value.resolve(), root.resolve()
    return str(value.relative_to(root)) if value.is_relative_to(root) else str(value)


def _decode_header(payload: dict) -> dict:
    params = payload.get("params")
    if not isinstance(params, list) or len(params) != 2 or params[1] is not False:
        raise DataContractError("Observation header requires a numbered, non-expanded block request")
    number = parse_integer(params[0])
    raw = rpc_call_value(payload, method="eth_getBlockByNumber", params=[hex(number), False])
    if not isinstance(raw, dict) or parse_integer(raw.get("number")) != number or number < 0:
        raise DataContractError("Observation header response block number mismatch")
    result = {"block_number": number, "timestamp": parse_integer(raw.get("timestamp"))}
    if result["timestamp"] < 0:
        raise DataContractError("Observation header timestamp must be nonnegative")
    for source, target in (("hash", "block_hash"), ("parentHash", "parent_hash")):
        value = raw.get(source)
        if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
            raise DataContractError("Observation header requires block and parent hashes")
        result[target] = value.lower()
    return result


def _same_header(left: dict, right: dict) -> bool:
    return all(left[key] == right[key] for key in ("block_number", "block_hash", "parent_hash", "timestamp"))


def _check_chain(headers: dict[int, dict]) -> None:
    ordered = sorted(headers.values(), key=lambda row: row["block_number"])
    for left, right in zip(ordered, ordered[1:]):
        if left["timestamp"] >= right["timestamp"]:
            raise DataContractError("Observation header timestamps are not strictly increasing")
        if right["block_number"] == left["block_number"] + 1 and right["parent_hash"] != left["block_hash"]:
            raise DataContractError("Adjacent observation headers have inconsistent parent hashes")


def _check_manifest_headers(headers: dict[int, dict], manifest: dict) -> None:
    for declared in manifest["block_headers"]:
        header = headers.get(parse_integer(declared["block_number"]))
        if header is None or any(header[key] != declared[key] for key in ("block_number", "block_hash", "timestamp")):
            raise DataContractError("Observation raw header is missing or differs from the original export")
    _check_chain(headers)


def _scan_cache(cache: Path, root: Path) -> dict[int, dict]:
    headers = {}
    for path in sorted(cache.glob("*.json")):
        payload = json.loads(path.read_text())
        if payload.get("method") != "eth_getBlockByNumber":
            continue
        header = _decode_header(payload)
        number = header["block_number"]
        if number in headers and not _same_header(headers[number], header):
            raise DataContractError("Conflicting raw headers for the same block number")
        headers.setdefault(number, {**header, "evidence_path": _relative(root, path),
                                    "evidence_sha256": sha256_file(path)})
    _check_chain(headers)
    return headers


def _targets(cfg: dict) -> list[dict]:
    result = []
    timing = cfg["timing"]
    for decision in decisions(cfg):
        for label, delay in (("decision", timing["block_delay_seconds"]), ("decision_delay_60s", 60)):
            result.append({"kind": label, "query_time": decision, "delay_seconds": delay})
        entry = decision + timing["execution_delay_seconds"]
        exit_time = entry + cfg["pilot"]["holding_seconds"]
        for label, stamp in (("entry", entry), ("exit", exit_time)):
            result.append({"kind": label, "query_time": stamp, "delay_seconds": 0})
        for stamp in benchmark_grid(entry, exit_time, timing["benchmark_seconds"])[:-1]:
            result.append({"kind": "benchmark_left", "query_time": stamp, "delay_seconds": 0})
    return result


def _target_checks(headers: dict[int, dict], targets: list[dict], first: int, end: int,
                   maximum_age: float) -> list[dict]:
    rows = sorted((row for number, row in headers.items() if first <= number < end),
                  key=lambda row: row["timestamp"])
    stamps = [row["timestamp"] for row in rows]
    result = []
    for target in targets:
        cutoff = target["query_time"] - target["delay_seconds"]
        index = bisect_right(stamps, cutoff) - 1
        selected = rows[index] if index >= 0 else None
        age = target["query_time"] - selected["timestamp"] if selected else None
        result.append({**target, "cutoff_time": cutoff,
                       "observation_block_number": selected["block_number"] if selected else None,
                       "observation_timestamp": selected["timestamp"] if selected else None,
                       "observation_age_seconds": age,
                       "status": "PASS" if age is not None and age <= maximum_age else "BLOCKED"})
    return result


def _fetch_latest(client, headers: dict[int, dict], cutoff: float, root: Path,
                  first: int, end: int) -> None:
    ordered = sorted((row for number, row in headers.items() if first <= number <= end),
                     key=lambda row: row["timestamp"])
    index = bisect_right([row["timestamp"] for row in ordered], cutoff) - 1
    if index < 0 or index + 1 >= len(ordered):
        raise RpcError("Missing cached block bounds for the requested observation cutoff")
    lower, upper = ordered[index], ordered[index + 1]
    while upper["block_number"] - lower["block_number"] > 1:
        number = (lower["block_number"] + upper["block_number"]) // 2
        if number not in headers:
            params = [hex(number), False]
            client.call("eth_getBlockByNumber", params)
            reference = client.evidence_reference("eth_getBlockByNumber", params)
            path = Path(reference["path"])
            header = _decode_header(json.loads(path.read_text()))
            headers[number] = {**header, "evidence_path": _relative(root, path),
                               "evidence_sha256": reference["sha256"]}
        middle = headers[number]
        if not lower["timestamp"] < middle["timestamp"] < upper["timestamp"]:
            raise DataContractError("New observation header is inconsistent with cached search bounds")
        if middle["timestamp"] <= cutoff:
            lower = middle
        else:
            upper = middle
    _check_chain(headers)


def _summary(headers: list[dict], first: int, end: int) -> dict:
    count = sum(first <= row["block_number"] < end for row in headers)
    return {"covered_blocks": end - first, "observation_header_count": count,
            "blocks_without_observation_headers": end - first - count}


def prepare_observation_headers(cfg: dict, *, allow_network: bool = False) -> dict:
    """Reuse raw headers, fetching only stale evaluation cutoffs when authorized."""
    root = Path(cfg["_root"])
    export_path, events_path = data_path(cfg, "export_manifest_path"), data_path(cfg, "events_path")
    _, manifest = load_rpc_export(events_path, export_path, root=root, pool_address=cfg["pool"]["address"])
    headers = _scan_cache(_path(root, cfg["network"]["cache_dir"]), root)
    _check_manifest_headers(headers, manifest)
    first, end = manifest["block_start"], manifest["block_end_exclusive"]
    # Keep boundary proofs too; the end header brackets the last covered block.
    headers = {number: row for number, row in headers.items() if first - 1 <= number <= end}
    targets, maximum_age = _targets(cfg), cfg["timing"]["max_pool_age_seconds"]
    missing = [row for row in _target_checks(headers, targets, first, end, maximum_age)
               if row["status"] == "BLOCKED"]
    usage, error = {"logical_requests": 0, "http_requests": 0, "estimated_compute_units": 0,
                    "downloaded_bytes": 0, "cache_hits": 0}, None
    if allow_network and missing:
        client = make_rpc_client(cfg)
        client.budget.max_requests = min(client.budget.max_requests, 512)
        client.budget.max_bytes = min(client.budget.max_bytes, 64_000_000)
        client.max_compute_units = min(client.max_compute_units or 512 * 20, 512 * 20)
        try:
            for cutoff in sorted({row["cutoff_time"] for row in missing}):
                _fetch_latest(client, headers, cutoff, root, first, end)
        except RpcError as exc:
            error = str(exc)
        usage = client.statistics()
    _check_manifest_headers(headers, manifest)
    checks = _target_checks(headers, targets, first, end, maximum_age)
    rows = [headers[number] for number in sorted(headers)]
    summary = _summary(rows, first, end)
    index = {"source": "ethereum_rpc_observation_headers", "schema_version": 1,
             "chain_id": 1, "pool_address": cfg["pool"]["address"].lower(),
             "created_at": datetime.now(timezone.utc).isoformat(),
             "export_manifest_path": _relative(root, export_path), "export_manifest_sha256": sha256_file(export_path),
             "events_path": _relative(root, events_path), "events_sha256": sha256_file(events_path),
             "block_start": first, "block_end_exclusive": end, "headers": rows,
             "status": "BLOCKED" if any(row["status"] == "BLOCKED" for row in checks) else "PASS"}
    path = events_path.parent / "observation_headers.json"
    write_json(path, index)
    result = {"status": index["status"], "path": _relative(root, path), "usage": usage,
              "target_checks": checks, "summary": summary}
    if error:
        result["error"] = error
    return result


def load_observation_headers(path: str | Path, *, root: str | Path,
                             export_manifest_path: str | Path) -> tuple[list[dict], dict]:
    """Validate derived observations against original logs and each raw header."""
    root = Path(root)
    path, export_path = _path(root, path), _path(root, export_manifest_path)
    index = json.loads(path.read_text())
    if index.get("source") != "ethereum_rpc_observation_headers" or index.get("schema_version") != 1:
        raise DataContractError("Invalid observation header index source or schema")
    if index.get("chain_id") != 1 or _path(root, index["export_manifest_path"]).resolve() != export_path.resolve():
        raise DataContractError("Observation header index chain or original export path mismatch")
    if index.get("export_manifest_sha256") != sha256_file(export_path):
        raise DataContractError("Observation header original export checksum mismatch")
    events_path = _path(root, index["events_path"])
    if index.get("events_sha256") != sha256_file(events_path):
        raise DataContractError("Observation header original events checksum mismatch")
    _, manifest = load_rpc_export(events_path, export_path, root=root, pool_address=index["pool_address"])
    first, end = manifest["block_start"], manifest["block_end_exclusive"]
    if (index["block_start"], index["block_end_exclusive"]) != (first, end):
        raise DataContractError("Observation header coverage differs from original export")
    headers = {}
    for row in index["headers"]:
        header = _decode_header(checked_artifact(row, root))
        number = header["block_number"]
        if number in headers or not first - 1 <= number <= end or not _same_header(row, header):
            raise DataContractError("Duplicate, out-of-coverage or inconsistent observation header")
        headers[number] = {**header, "evidence_path": row["evidence_path"],
                           "evidence_sha256": row["evidence_sha256"]}
    _check_manifest_headers(headers, manifest)
    rows = [headers[number] for number in sorted(headers) if first <= number < end]
    return rows, {**_summary(rows, first, end), "index_path": str(path), "index_sha256": sha256_file(path),
                  "export_manifest_sha256": sha256_file(export_path), "raw_log_reconciliation": "verified"}

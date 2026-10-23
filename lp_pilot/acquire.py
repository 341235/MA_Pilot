"""Acquire and cache the historical RPC inputs."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from .config import data_path, decisions, required_interval
from .data import normalize_event, parse_integer
from .rpc import RpcClient, RpcError, verify_pool_identity
from .storage import sha256, write_json, write_table


def _relative(cfg: dict, path: Path) -> str:
    path = path.resolve()
    root = Path(cfg["_root"]).resolve()
    return str(path.relative_to(root)) if path.is_relative_to(root) else str(path)


def make_rpc_client(cfg: dict) -> RpcClient:
    endpoint = os.environ.get("ARCHIVE_RPC_URL") or os.environ.get("ETH_RPC_URL")
    if not endpoint:
        raise RpcError("Set ETH_RPC_URL or ARCHIVE_RPC_URL in the current process environment")
    limits = cfg["acquisition"]
    return RpcClient(endpoint, cache_dir=Path(cfg["_root"]) / cfg["network"]["cache_dir"],
                     timeout=cfg["network"]["timeout_seconds"], retries=1,
                     max_requests=limits["max_requests"], max_bytes=limits["max_download_bytes"],
                     max_compute_units=limits["max_compute_units"], cu_per_second=limits["cu_per_second"])


def _save_snapshot(cfg: dict, client: RpcClient, block: int) -> dict:
    from .rpc_snapshot import fetch_full_snapshot
    path = data_path(cfg, "snapshot_path")
    result = fetch_full_snapshot(client, block, pool_address=cfg["pool"]["address"],
                                 tick_spacing=cfg["pool"]["tick_spacing"], fee_pips=cfg["pool"]["fee_pips"],
                                 batch_size=cfg["acquisition"]["batch_size"],
                                 max_initialized_ticks=cfg["acquisition"]["max_initialized_ticks"])
    evidence = result.pop("evidence")
    evidence_path = path.with_name("snapshot_evidence.json")
    write_json(evidence_path, evidence)
    write_json(path, result)
    words = sorted(int(word) for word in result["bitmap_words"])
    manifest = {"source": "ethereum_rpc_snapshot", "block_number": block, "block_hash": result["block_hash"],
                "snapshot_sha256": sha256(path), "evidence_path": _relative(cfg, evidence_path),
                "evidence_sha256": sha256(evidence_path), "bitmap_word_min": min(words),
                "bitmap_word_max": max(words), "bitmap_words_count": len(words),
                "initialized_tick_count": len(result["ticks"]), "complete": True,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "source_note": "Historical eth_call pinned to canonical block hash; same RPC provider as logs"}
    write_json(str(path) + ".manifest.json", manifest)
    return result


def _save_checkpoints(cfg: dict, client: RpcClient, events: list[dict], block_end: int) -> int:
    from .rpc_snapshot import fetch_state_checkpoint
    # End of complete blocks, never an intermediate event within the same block.
    blocks = sorted({int(row["block_number"]) for row in events}) or [block_end - 1]
    count = min(cfg["acquisition"]["checkpoint_count"], len(blocks))
    selected = sorted({blocks[(index + 1) * len(blocks) // count - 1] for index in range(count)}) if count else []
    checkpoints = []
    path = data_path(cfg, "checkpoints_path")
    for block in selected:
        point = fetch_state_checkpoint(client, block, pool_address=cfg["pool"]["address"],
                                       batch_size=cfg["acquisition"]["batch_size"])
        evidence = point.pop("evidence")
        evidence_path = path.parent / "checkpoint_evidence" / f"{block}.json"
        write_json(evidence_path, evidence)
        point.update(source_kind="historical_rpc", evidence_path=_relative(cfg, evidence_path),
                     evidence_sha256=sha256(evidence_path),
                     independence_note="Independent RPC method (eth_call vs logs), same provider; not a second-provider check")
        checkpoints.append(point)
    write_json(path, {"checkpoints": checkpoints})
    return len(checkpoints)


def _receipt_sample(cfg: dict, by_hash: dict[str, list[dict]]) -> list[str]:
    """Sample a past swap for each decision, then fill with LP operations.

    This supplies trailing gas observations within the receipt budget. The
    sample mean need not represent gas usage across all pool transactions.
    """
    from bisect import bisect_right
    swaps = sorted((int(row["timestamp"]), tx) for tx, rows in by_hash.items()
                   for row in rows if row["event_type"] == "Swap")
    stamps = [row[0] for row in swaps]
    chosen = []
    for decision in decisions(cfg):
        index = bisect_right(stamps, decision - cfg["timing"]["block_delay_seconds"]) - 1
        if index >= 0 and swaps[index][1] not in chosen:
            chosen.append(swaps[index][1])
    preferred = sorted(by_hash, key=lambda tx: (not any(row["event_type"] in {"Mint", "Burn", "Collect"} for row in by_hash[tx]),
                                               int(by_hash[tx][0]["block_number"]), tx))
    chosen.extend(tx for tx in preferred if tx not in chosen)
    return chosen[:cfg["acquisition"]["max_receipts"]]


def _save_receipts(cfg: dict, client: RpcClient, events: list[dict]) -> dict:
    """Verify sampled receipts and retain their whole-transaction gas costs."""
    by_hash: dict[str, list[dict]] = {}
    for row in events:
        by_hash.setdefault(row["transaction_hash"].lower(), []).append(row)
    chosen = _receipt_sample(cfg, by_hash)
    receipts, verified = [], 0
    for offset in range(0, len(chosen), cfg["acquisition"]["batch_size"]):
        batch = chosen[offset:offset + cfg["acquisition"]["batch_size"]]
        responses = client.batch_call([("eth_getTransactionReceipt", [tx]) for tx in batch])
        for tx, receipt in zip(batch, responses):
            required = {"transactionHash", "status", "blockNumber", "blockHash", "gasUsed", "effectiveGasPrice", "logs"}
            if not isinstance(receipt, dict) or not required.issubset(receipt):
                raise RpcError("Historical receipt lacks required transaction, gas or log evidence")
            if not receipt or receipt.get("transactionHash", "").lower() != tx or parse_integer(receipt.get("status")) != 1:
                raise RpcError("Historical pool transaction receipt is absent, failed or mismatched")
            source_events = by_hash[tx]
            if (parse_integer(receipt["blockNumber"]) != int(source_events[0]["block_number"])
                    or receipt["blockHash"].lower() != source_events[0]["block_hash"].lower()):
                raise RpcError("Receipt block identity disagrees with downloaded logs")
            decoded = {}
            for log in receipt.get("logs", []):
                if log.get("address", "").lower() == cfg["pool"]["address"].lower():
                    event = normalize_event({**log, "timestamp": source_events[0]["timestamp"]})
                    decoded[event["log_index"]] = event
            for event in source_events:
                match = decoded.get(int(event["log_index"]))
                fields = {"event_type", "amount0", "amount1", "sqrt_price_x96", "tick", "liquidity", "amount", "tick_lower", "tick_upper", "paid0", "paid1"}
                if match is None or any(match.get(key) != event.get(key) for key in fields):
                    raise RpcError("Receipt log sample disagrees with event export")
                verified += 1
            evidence_path = data_path(cfg, "receipts_path").parent / "receipt_evidence" / f"{tx}.json"
            write_json(evidence_path, receipt)
            receipts.append({"transaction_hash": tx, "timestamp": source_events[0]["timestamp"],
                             "block_number": source_events[0]["block_number"], "block_hash": source_events[0]["block_hash"],
                             "gas_used": parse_integer(receipt["gasUsed"]),
                             "effective_gas_price": parse_integer(receipt["effectiveGasPrice"]), "status": 1,
                             "operations": sorted({row["event_type"].lower() for row in source_events}),
                             "single_position": False, "evidence_path": _relative(cfg, evidence_path),
                             "evidence_sha256": sha256(evidence_path),
                             "attribution_note": "Pool logs do not prove a transaction contains only one operation/position; gas remains transaction-level"})
    write_json(data_path(cfg, "receipts_path"), {"receipts": receipts})
    return {"sampled_transactions": len(receipts), "verified_pool_events": verified,
            "complete_transaction_coverage": len(chosen) == len(by_hash),
            "selection": "Available predecision swaps for gas, then LP operations; deduplicated and bounded",
            "operation_cost_calibration": "BLOCKED: ambiguous transaction/position attribution"}


def fetch_rpc(cfg: dict, progress=None) -> dict:
    from .rpc_events import download_events
    if cfg["data"].get("source") != "rpc":
        raise ValueError("fetch-rpc requires data.source='rpc'; no source substitution is implicit")
    first, last = required_interval(cfg)
    client = make_rpc_client(cfg)
    started = time.perf_counter()
    result = {"coverage_start": first, "coverage_end": last, "status": "BLOCKED"}
    def announce(stage, **values):
        if progress:
            progress({"stage": stage, **values, "usage": client.statistics()})
    try:
        identity = verify_pool_identity(client, cfg["pool"]["address"])
        if identity["status"] != "PASS":
            raise RpcError("Configured pool identity does not match Ethereum contract calls")
        probe_path = Path(cfg["_root"]) / "data/input/rpc/access_probe.json"
        probe = json.loads(probe_path.read_text()) if probe_path.exists() else {}
        probe.setdefault("rpc", {})["pool_identity"] = {"status": identity["status"], "value": identity}
        probe["rpc"]["identity_retrieved_at"] = datetime.now(timezone.utc).isoformat()
        write_json(probe_path, probe)
        announce("events")
        events, manifest = download_events(client, first, last, pool_address=cfg["pool"]["address"],
                                           chunk_blocks=cfg["acquisition"]["chunk_blocks"], progress=progress)
        path = data_path(cfg, "events_path")
        write_table(path, events, empty_columns=("timestamp", "block_number"))
        manifest.update(sha256=sha256(path), retrieved_at=datetime.now(timezone.utc).isoformat(),
                        source="ethereum_rpc", chain_id=1, pool_address=cfg["pool"]["address"].lower())
        write_json(data_path(cfg, "export_manifest_path"), manifest)
        result.update(event_count=len(events), event_types=manifest.get("event_types"),
                      block_start=manifest["block_start"], block_end_exclusive=manifest["block_end_exclusive"])
        snapshot_block = int(events[0]["block_number"]) - 1 if events else manifest["block_start"] - 1
        announce("full_snapshot", block=snapshot_block)
        snapshot = _save_snapshot(cfg, client, snapshot_block)
        result.update(snapshot_block=snapshot_block, initialized_ticks=len(snapshot["ticks"]), bitmap_words=len(snapshot["bitmap_words"]))
        announce("checkpoints")
        result["checkpoint_count"] = _save_checkpoints(cfg, client, events, manifest["block_end_exclusive"])
        announce("receipts")
        result["receipt_checks"] = _save_receipts(cfg, client, events)
        result.update(status="PASS", generated_input_dir=_relative(cfg, path.parent))
    except (RpcError, ValueError, OSError) as error:
        result.update(status="BLOCKED" if isinstance(error, RpcError) else "FAIL", error_type=type(error).__name__, detail=str(error))
    result.update(usage=client.statistics(), wall_seconds=round(time.perf_counter() - started, 6))
    return result

"""Historical pool logs, cached in bounded block intervals.

Coverage records the queried intervals and returned logs from one provider.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable

from .data import POOL, DataContractError, decode_pool_log, normalize_event, order_events, parse_integer, utc_seconds
from .rpc import RpcClient, RpcError


def _number(value: Any, name: str) -> int:
    try:
        number = parse_integer(value)
    except DataContractError:
        raise RpcError(f"Invalid RPC {name}") from None
    if number < 0:
        raise RpcError(f"Negative RPC {name}")
    return number


def _hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise RpcError(f"Invalid RPC {name}")
    return value.lower()


def _header(value: Any, requested: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RpcError("Historical block header unavailable")
    number = _number(value.get("number"), "block number")
    if number != requested:
        raise RpcError("Historical block header number differs from request")
    return {"block_number": number, "block_hash": _hash(value.get("hash"), "block hash"),
            "timestamp": _number(value.get("timestamp"), "block timestamp")}


def _get_header(client: RpcClient, number: int) -> dict[str, Any]:
    return _header(client.call("eth_getBlockByNumber", [hex(number), False]), number)


def find_block_at_or_after(client: RpcClient, timestamp: Any, *, low: int | None = None,
                           high: int | None = None) -> int:
    """Find the first historical block at or after timestamp.

    Search bounds are inclusive. Validate the preceding block too, since a
    supplied lower bound might skip an earlier match. Reject future timestamps.
    """
    target = utc_seconds(timestamp)
    first = 0 if low is None else _number(low, "search lower bound")
    last = _number(client.call("eth_blockNumber"), "head block") if high is None else _number(high, "search upper bound")
    if last < first:
        raise RpcError("Invalid historical block search interval")
    headers: dict[int, dict[str, Any]] = {}

    def get(number: int) -> dict[str, Any]:
        if number not in headers:
            headers[number] = _get_header(client, number)
        return headers[number]

    if get(last)["timestamp"] < target:
        raise RpcError("Requested timestamp is beyond the historical search head")
    if first and get(first - 1)["timestamp"] >= target:
        raise RpcError("Search lower bound excludes an earlier qualifying block")
    while first < last:
        middle = (first + last) // 2
        if get(middle)["timestamp"] < target:
            first = middle + 1
        else:
            last = middle
    if get(first)["timestamp"] < target or (first and get(first - 1)["timestamp"] >= target):
        raise RpcError("Historical timestamp boundary could not be established")
    return first


def _headers(client: RpcClient, numbers: list[int]) -> dict[int, dict[str, Any]]:
    """Fetch at most ten headers per JSON-RPC batch, preserving request order."""
    output = {}
    for offset in range(0, len(numbers), 10):
        batch = numbers[offset:offset + 10]
        calls = [("eth_getBlockByNumber", [hex(number), False]) for number in batch]
        values = client.batch_call(calls) if hasattr(client, "batch_call") else [client.call(method, params) for method, params in calls]
        if not isinstance(values, list) or len(values) != len(batch):
            raise RpcError("Historical block header batch result count mismatch")
        for number, value in zip(batch, values):
            output[number] = _header(value, number)
    return output


def download_events(client: RpcClient, start: Any, end: Any, *, pool_address: str = POOL,
                    chunk_blocks: int = 10,
                    progress: Callable[[dict[str, Any]], None] | None = None
                    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Download all pool topics in the half-open UTC interval.

    Query at most ten blocks at a time and record empty intervals as well.
    The RPC client handles retries, caching and acquisition budgets.
    """
    if isinstance(chunk_blocks, bool) or not isinstance(chunk_blocks, int) or not 1 <= chunk_blocks <= 10:
        raise RpcError("Log chunk size must be between one and ten blocks")
    if not isinstance(pool_address, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", pool_address):
        raise RpcError("Invalid pool address")
    pool_address = pool_address.lower()
    coverage_start, coverage_end = utc_seconds(start), utc_seconds(end)
    if coverage_start >= coverage_end:
        raise RpcError("Historical event interval must be nonempty")
    if _number(client.call("eth_chainId"), "chain id") != 1:
        raise RpcError("Pool event download requires Ethereum mainnet")
    block_start = find_block_at_or_after(client, coverage_start)
    block_end = find_block_at_or_after(client, coverage_end, low=block_start)
    boundary_numbers = sorted({block_start, block_end, max(0, block_start - 1), max(0, block_end - 1)})
    boundary_headers = _headers(client, boundary_numbers)
    for number, target in ((block_start, coverage_start), (block_end, coverage_end)):
        if boundary_headers[number]["timestamp"] < target or (number and boundary_headers[number - 1]["timestamp"] >= target):
            raise RpcError("Historical timestamp boundary changed during download")
    total_chunks = (block_end - block_start + chunk_blocks - 1) // chunk_blocks
    events: list[dict[str, Any]] = []
    chunks = []
    headers: dict[int, dict[str, Any]] = dict(boundary_headers)
    for first in range(block_start, block_end, chunk_blocks):
        last = min(first + chunk_blocks - 1, block_end - 1)
        params = [{"address": pool_address, "fromBlock": hex(first), "toBlock": hex(last)}]
        logs = client.call("eth_getLogs", params)
        if not isinstance(logs, list) or any(not isinstance(log, dict) for log in logs):
            raise RpcError("Historical log result must be a list of objects")
        numbers = []
        for log in logs:
            number = _number(log.get("blockNumber"), "log block number")
            if not first <= number <= last:
                raise RpcError("Provider returned a log outside the requested block interval")
            numbers.append(number)
        headers.update(_headers(client, sorted(set(numbers) - headers.keys())))
        for log, number in zip(logs, numbers):
            header = headers[number]
            if _hash(log.get("blockHash"), "log block hash") != header["block_hash"]:
                raise RpcError("Log block hash differs from historical header")
            if not coverage_start <= header["timestamp"] < coverage_end:
                raise RpcError("Log timestamp lies outside the requested UTC interval")
            # Keep the original topics and data alongside the decoded integers.
            events.append(normalize_event({**log, **decode_pool_log(log), "timestamp": header["timestamp"]}, pool_address))
        chunk = {"from_block": first, "to_block": last, "row_count": len(logs),
                 "raw_logs_sha256": hashlib.sha256(json.dumps(logs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
        if hasattr(client, "evidence_reference"):
            evidence = client.evidence_reference("eth_getLogs", params)
            if evidence:
                chunk.update(evidence_path=evidence["path"], evidence_sha256=evidence["sha256"])
        chunks.append(chunk)
        if progress is not None:
            progress({"stage": "logs", "completed_chunks": len(chunks), "total_chunks": total_chunks,
                      "from_block": first, "to_block": last, "raw_log_count": len(events)})
    # Keep duplicates as errors so reconciliation still matches the raw response.
    ordered = order_events(events)
    manifest = {
        "source": "ethereum_rpc", "schema_version": 1, "chain_id": 1,
        "pool_address": pool_address, "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "coverage_start": coverage_start, "coverage_end": coverage_end,
        "block_start": block_start, "block_end_exclusive": block_end,
        "boundary_headers": [boundary_headers[number] for number in boundary_numbers],
        "block_headers": [headers[number] for number in sorted(headers)],
        "chunk_blocks": chunk_blocks, "chunks": chunks, "coverage_complete": True,
        "source_row_count": sum(chunk["row_count"] for chunk in chunks),
        "normalized_row_count": len(ordered), "duplicates_removed": 0,
        "hash_checks": len(ordered), "event_types": sorted({row["event_type"] for row in ordered}),
        "unknown_event_count": sum(row["event_type"] == "Unknown" for row in ordered),
        "completeness_caveat": "All disjoint bounded block intervals were queried successfully, including empty results. Log/header hash checks and raw-result counts use the same RPC provider; independent completeness is not established.",
    }
    return ordered, manifest

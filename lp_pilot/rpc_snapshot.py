"""Historical pool snapshots pinned to a canonical block hash.

A full snapshot covers every bitmap word containing a usable tick.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from .data import POOL
from .replay import PoolState, ReplayError
from .rpc import RpcClient, RpcError, SELECTORS, USDC, WETH, _signed
from .uniswap import MAX_TICK, MIN_TICK


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RpcError(f"{name} must be a positive integer")
    return value


def bitmap_word_bounds(tick_spacing: int = 10) -> tuple[int, int]:
    """Inclusive bitmap word bounds, with signed floor division as in v3."""
    _positive_integer(tick_spacing, "tick spacing")
    if tick_spacing > 16384:
        raise RpcError("Tick spacing exceeds the supported protocol range")
    minimum_compressed = -((-MIN_TICK) // tick_spacing)
    maximum_compressed = MAX_TICK // tick_spacing
    return minimum_compressed // 256, maximum_compressed // 256


def _words(result: Any, count: int, name: str) -> list[int]:
    if not isinstance(result, str) or not result.startswith("0x") or len(result) != 2 + 64 * count:
        raise RpcError(f"{name} historical ABI shape mismatch")
    if any(character not in "0123456789abcdefABCDEF" for character in result[2:]):
        raise RpcError(f"{name} historical result is not hexadecimal")
    try:
        return [int(result[index:index + 64], 16) for index in range(2, len(result), 64)]
    except ValueError:
        raise RpcError(f"{name} historical result is not hexadecimal") from None


def _unsigned(value: int, bits: int, name: str) -> int:
    if not 0 <= value < 1 << bits:
        raise RpcError(f"{name} ABI integer outside uint{bits}")
    return value


def _header(client: RpcClient, block: int) -> dict[str, Any]:
    # Read again from the provider to check canonicality.
    header = client.call("eth_getBlockByNumber", [hex(block), False], use_cache=False)
    if not isinstance(header, dict):
        raise RpcError("Historical block header unavailable")
    try:
        number, timestamp = int(header["number"], 16), int(header["timestamp"], 16)
        block_hash = header["hash"]
        if not isinstance(block_hash, str) or len(block_hash) != 66 or not block_hash.startswith("0x"):
            raise ValueError
        int(block_hash[2:], 16)
    except (KeyError, TypeError, ValueError):
        raise RpcError("Malformed historical block header") from None
    if number != block or timestamp < 0:
        raise RpcError("Historical block header disagrees with requested block")
    return header


def _read(
    client: RpcClient,
    requests: list[tuple[str, int | None, int]],
    *,
    pool_address: str,
    block_identifier: dict[str, Any],
    batch_size: int,
    evidence: dict[str, Any],
) -> list[list[int]]:
    decoded: list[list[int]] = []
    for offset in range(0, len(requests), batch_size):
        chunk = requests[offset:offset + batch_size]
        batch = []
        for name, argument, _count in chunk:
            selector = SELECTORS[name]
            if argument is not None:
                selector += format(argument % (1 << 256), "064x")
            batch.append(("eth_call", [{"to": pool_address, "data": selector}, block_identifier]))
        results = client.batch_call(batch)
        if not isinstance(results, list) or len(results) != len(batch):
            raise RpcError("Historical RPC batch has missing or extra results")
        for (name, argument, count), (method, params), result in zip(chunk, batch, results):
            values = _words(result, count, name)
            record = {"method": method, "params": params, "result": result}
            if argument is None:
                evidence["calls"][name] = record
            else:
                evidence["calls"].setdefault(name, {})[str(argument)] = record
            decoded.append(values)
    return decoded


def _start(client: RpcClient, block: int, pool_address: str, batch_size: int) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(block, bool) or not isinstance(block, int) or block < 0:
        raise RpcError("Historical snapshot requires a non-negative block number")
    _positive_integer(batch_size, "batch size")
    if batch_size > 10:
        raise RpcError("Historical snapshot batches are limited to ten calls")
    if pool_address.lower() != POOL:
        raise RpcError("Historical pilot snapshots require the configured USDC/WETH pool")
    chain = client.call("eth_chainId")
    try:
        chain_id = int(chain, 16)
    except (TypeError, ValueError):
        raise RpcError("Malformed Ethereum chain ID") from None
    if chain_id != 1:
        raise RpcError("Historical pilot snapshots require Ethereum mainnet")
    before = _header(client, block)
    block_identifier = {"blockHash": before["hash"], "requireCanonical": True}
    evidence: dict[str, Any] = {
        "source_kind": "historical_rpc", "chain_id": 1, "pool_address": POOL,
        "block_number": block, "block_hash": before["hash"],
        "block_identifier": block_identifier,
        "provider": "configured Ethereum HTTP RPC (endpoint redacted)",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "block_header_before": before, "calls": {},
    }
    requests = [("slot0", None, 7), ("liquidity", None, 1),
                ("feeGrowthGlobal0X128", None, 1), ("feeGrowthGlobal1X128", None, 1)]
    slot, liquidity, growth0, growth1 = _read(
        client, requests, pool_address=POOL, block_identifier=block_identifier,
        batch_size=batch_size, evidence=evidence,
    )
    _unsigned(slot[0], 160, "sqrt price")
    tick = _signed(slot[1], 24)
    for value in slot[2:5]:
        _unsigned(value, 16, "slot0 observation field")
    _unsigned(slot[5], 8, "protocol fee")
    if slot[6] not in (0, 1):
        raise RpcError("slot0 unlocked flag is not an ABI boolean")
    _unsigned(liquidity[0], 128, "active liquidity")
    protocol0, protocol1 = slot[5] & 15, slot[5] >> 4
    if any(fee != 0 and not 4 <= fee <= 10 for fee in (protocol0, protocol1)):
        raise RpcError("Historical protocol fee must be zero or 4..10")
    snapshot = {
        "chain_id": 1, "pool_address": POOL,
        "block_number": block, "block_hash": before["hash"],
        "timestamp": int(before["timestamp"], 16),
        "sqrt_price_x96": str(slot[0]), "tick": tick, "liquidity": str(liquidity[0]),
        "fee_protocol0": protocol0, "fee_protocol1": protocol1,
        "fee_growth_global0_x128": str(growth0[0]),
        "fee_growth_global1_x128": str(growth1[0]),
        "ticks": {}, "bitmap_words": {}, "ticks_complete": False,
        "provenance": "independent historical eth_call pinned to canonical block hash",
        "source": "independent historical eth_call", "evidence": evidence,
    }
    return snapshot, evidence


def _tick(values: list[int], *, require_initialized: bool) -> dict[str, Any]:
    _unsigned(values[0], 128, "tick gross liquidity")
    liquidity_net = _signed(values[1], 128)
    _signed(values[4], 56)
    _unsigned(values[5], 160, "seconds per liquidity outside")
    _unsigned(values[6], 32, "seconds outside")
    if values[7] not in (0, 1):
        raise RpcError("Historical tick initialized flag is not an ABI boolean")
    if bool(values[0]) != bool(values[7]):
        raise RpcError("Historical tick gross liquidity disagrees with initialized flag")
    if abs(liquidity_net) > values[0]:
        raise RpcError("Historical tick net liquidity exceeds gross liquidity")
    if require_initialized and (values[7] != 1 or values[0] == 0):
        raise RpcError("Bitmap set bit lacks an initialized nonzero-liquidity tick")
    return {"liquidity_gross": str(values[0]), "liquidity_net": str(liquidity_net),
            "fee_growth_outside0_x128": str(values[2]),
            "fee_growth_outside1_x128": str(values[3]), "initialized": bool(values[7])}


def _finish(client: RpcClient, block: int, evidence: dict[str, Any]) -> None:
    after = _header(client, block)
    before = evidence["block_header_before"]
    if after["hash"].lower() != before["hash"].lower() or after["timestamp"] != before["timestamp"]:
        raise RpcError("Historical canonical block changed while fetching state")
    evidence["block_header_after"] = after
    evidence["status"] = "PASS"


def fetch_full_snapshot(
    client: RpcClient,
    block: int,
    *,
    pool_address: str = POOL,
    tick_spacing: int = 10,
    fee_pips: int = 500,
    batch_size: int = 10,
    max_initialized_ticks: int = 5000,
) -> dict[str, Any]:
    """Fetch the complete end-of-block tick distribution.

    Pin every contract read with EIP-1898 and check fresh headers before and
    after acquisition. Only mark the snapshot complete after all reads pass.
    """
    if tick_spacing != 10 or fee_pips != 500:
        raise RpcError("Historical pilot snapshots require tick spacing 10 and fee 500")
    _positive_integer(max_initialized_ticks, "initialized tick limit")
    first_word, last_word = bitmap_word_bounds(tick_spacing)
    snapshot, evidence = _start(client, block, pool_address, batch_size)
    block_identifier = evidence["block_identifier"]
    immutable_names = ("tickSpacing", "fee", "token0", "token1")
    spacing, fee, token0, token1 = _read(
        client, [(name, None, 1) for name in immutable_names],
        pool_address=POOL, block_identifier=block_identifier, batch_size=batch_size, evidence=evidence,
    )
    if (spacing[0], fee[0], token0[0], token1[0]) != (tick_spacing, fee_pips, int(USDC, 16), int(WETH, 16)):
        raise RpcError("Historical pool immutable identity mismatch")
    snapshot.update(tick_spacing=tick_spacing, fee_pips=fee_pips)
    word_indices = list(range(first_word, last_word + 1))
    bitmaps = _read(
        client, [("tickBitmap", word, 1) for word in word_indices],
        pool_address=POOL, block_identifier=block_identifier, batch_size=batch_size, evidence=evidence,
    )
    min_usable = -((-MIN_TICK) // tick_spacing) * tick_spacing
    max_usable = MAX_TICK // tick_spacing * tick_spacing
    initialized = []
    for word, values in zip(word_indices, bitmaps):
        bitmap = values[0]
        snapshot["bitmap_words"][str(word)] = str(bitmap)
        for bit in range(256):
            if bitmap & (1 << bit):
                tick = (word * 256 + bit) * tick_spacing
                if not min_usable <= tick <= max_usable:
                    raise RpcError("Bitmap contains initialized bit outside usable tick range")
                initialized.append(tick)
    if len(initialized) > max_initialized_ticks:
        raise RpcError("Historical initialized tick limit exceeded; snapshot remains incomplete")
    tick_values = _read(
        client, [("ticks", tick, 8) for tick in initialized],
        pool_address=POOL, block_identifier=block_identifier, batch_size=batch_size, evidence=evidence,
    )
    for tick, values in zip(initialized, tick_values):
        snapshot["ticks"][str(tick)] = _tick(values, require_initialized=True)
    _finish(client, block, evidence)
    candidate = {**snapshot, "ticks_complete": True}
    try:
        state = PoolState.from_snapshot(candidate)
    except ReplayError as exc:
        raise RpcError(f"Historical full tick snapshot failed invariants: {exc}") from None
    evidence["bitmap_proof"] = {
        "first_word": first_word, "last_word": last_word,
        "expected_word_count": len(word_indices), "queried_word_count": len(bitmaps),
        "min_usable_tick": min_usable, "max_usable_tick": max_usable,
        "initialized_tick_count": len(initialized), "queried_tick_count": len(tick_values),
        "net_liquidity_sum": str(sum(tick.liquidity_net for tick in state.ticks.values())),
        "active_liquidity_sum": str(sum(row.liquidity_net for tick, row in state.ticks.items() if tick <= state.tick)),
    }
    candidate["coverage_note"] = "Every usable protocol tick bitmap word and every set-bit tick were fetched at one canonical block hash and validated."
    return candidate


def fetch_state_checkpoint(
    client: RpcClient,
    block: int,
    *,
    pool_address: str = POOL,
    selected_ticks: Iterable[int] = (),
    batch_size: int = 10,
) -> dict[str, Any]:
    """Read a partial checkpoint for endpoint and fee comparisons.

    Selected ticks may be uninitialized; this is not a complete tick map.
    """
    ticks = list(selected_ticks)
    if any(isinstance(tick, bool) or not isinstance(tick, int) or not MIN_TICK <= tick <= MAX_TICK or tick % 10 for tick in ticks):
        raise RpcError("Checkpoint tick is outside usable tick range or spacing")
    if len(set(ticks)) != len(ticks):
        raise RpcError("Checkpoint tick list contains duplicates")
    snapshot, evidence = _start(client, block, pool_address, batch_size)
    values = _read(
        client, [("ticks", tick, 8) for tick in ticks], pool_address=POOL,
        block_identifier=evidence["block_identifier"], batch_size=batch_size, evidence=evidence,
    )
    for tick, row in zip(ticks, values):
        snapshot["ticks"][str(tick)] = _tick(row, require_initialized=False)
    snapshot.update(tick_spacing=10, fee_pips=500)
    snapshot["coverage_note"] = "Independent endpoint and fee-growth checkpoint; only requested ticks were read."
    _finish(client, block, evidence)
    return snapshot

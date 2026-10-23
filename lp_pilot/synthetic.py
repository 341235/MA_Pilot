"""Deterministic artificial pool data for synthetic runs and tests.
"""
from __future__ import annotations

from decimal import Decimal
import hashlib

from .config import required_interval
from .uniswap import compute_swap_step, get_sqrt_ratio_at_tick, price_from_sqrt


def _hash(label: str) -> str:
    return "0x" + hashlib.sha256(label.encode()).hexdigest()


def generate(cfg: dict) -> tuple[dict, list[dict], list[dict], list[dict]]:
    start, end = required_interval(cfg)
    seed = cfg["pilot"]["synthetic_seed"]
    initial_tick, historical_liquidity = 200100, 10**19
    snapshot = {
        "block_number": 19000000, "block_hash": _hash(f"{seed}-snapshot"),
        "timestamp": start - 12, "sqrt_price_x96": get_sqrt_ratio_at_tick(initial_tick),
        "tick": initial_tick, "liquidity": historical_liquidity,
        "fee_pips": cfg["pool"]["fee_pips"], "tick_spacing": cfg["pool"]["tick_spacing"],
        "fee_protocol0": 0, "fee_protocol1": 0,
        "fee_growth_global0_x128": 0, "fee_growth_global1_x128": 0,
        "ticks_complete": True, "provenance": "synthetic",
        "ticks": [
            {"tick": 198000, "liquidity_gross": historical_liquidity, "liquidity_net": historical_liquidity},
            {"tick": 202000, "liquidity_gross": historical_liquidity, "liquidity_net": -historical_liquidity},
        ],
    }
    events, references, receipts = [], [], []
    sqrt = snapshot["sqrt_price_x96"]
    for i, timestamp in enumerate(range(start, end, 60)):
        # Oscillate across hypothetical ranges, inside one bitmap word.
        phase = (i + 61) % 240
        tick = initial_tick + (phase if phase <= 120 else 240 - phase) * 3 - 180
        target = get_sqrt_ratio_at_tick(tick)
        if target == sqrt:
            tick += 1
            target = get_sqrt_ratio_at_tick(tick)
        step = compute_swap_step(sqrt, target, historical_liquidity, 10**35, cfg["pool"]["fee_pips"])
        down = target < sqrt
        amount0 = step.amount_in + step.fee_amount if down else -step.amount_out
        amount1 = -step.amount_out if down else step.amount_in + step.fee_amount
        tx_hash = _hash(f"{seed}-transaction-{i}")
        events.append({
            "block_number": snapshot["block_number"] + i + 1,
            "block_hash": _hash(f"{seed}-block-{i}"), "timestamp": timestamp,
            "transaction_hash": tx_hash, "transaction_index": 0, "log_index": 0,
            "event_type": "Swap", "amount0": amount0, "amount1": amount1,
            "sqrt_price_x96": target, "tick": tick, "liquidity": historical_liquidity,
            "amount_specified": 10**35, "sqrt_price_limit_x96": target,
            "pool_address": cfg["pool"]["address"], "synthetic": True,
            "gas_price_wei": 30 * 10**9,
        })
        sqrt = target
        price = price_from_sqrt(sqrt)
        references.append({
            "timestamp": timestamp, "available_at": timestamp + cfg["timing"]["reference_delay_seconds"],
            "price_usdc_per_eth": price * (1 + Decimal((i % 11) - 5) / Decimal(100000)),
            "source": "synthetic_closed_minute_reference", "synthetic": True,
        })
        receipts.append({
            "transaction_hash": tx_hash, "gas_used": 150000,
            "effective_gas_price": 30 * 10**9, "timestamp": timestamp,
            "status": 1, "synthetic": True,
        })
    return snapshot, events, references, receipts

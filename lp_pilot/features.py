"""Trailing market features and provisional toxicity proxies."""
from __future__ import annotations

from decimal import Decimal
from math import log, sqrt
from statistics import mean
from typing import Any, Iterable, Mapping

from .costs import deduplicate_receipts
from .temporal import apply_availability, asof_index, backward_asof, reference_asof
from .uniswap import price_from_sqrt


def _settings(config: Mapping[str, Any]) -> dict:
    return {"block_delay_seconds": 12, "reference_delay_seconds": 1,
            "max_price_age_seconds": 120, "max_pool_age_seconds": 120,
            "markout_seconds": 300, "lookback_seconds": 3600,
            **config.get("timing", {})}


def historical_markout(events: Iterable[Mapping[str, Any]], references: Iterable[Mapping[str, Any]],
                       decision_time: float, *, lookback_seconds: float = 3600,
                       horizon_seconds: float = 300, max_price_age_seconds: float = 120,
                       reference_delay_seconds: float = 1) -> dict:
    """Quote-volume-weighted signed reference return, positive for taker gains.

    Swap direction is +1 for WETH bought by the taker (pool amount1 < 0).
    Both prices are external USDC/WETH. Pool execution prices and swap fees do
    not enter this proxy. Horizons and information availability must both mature.
    The trailing window is defined by *trade time*, not horizon completion time.
    """
    if horizon_seconds <= 0 or lookback_seconds <= 0:
        raise ValueError("Markout horizon and lookback must be positive")
    refs = asof_index(references)
    numerator, denominator, count, immature, missing = Decimal(0), Decimal(0), 0, 0, 0
    for event in events:
        timestamp = float(event["timestamp"])
        if event.get("event_type", "").lower() != "swap" or not decision_time - lookback_seconds < timestamp <= decision_time:
            continue
        if float(event.get("available_at", timestamp)) > decision_time:
            continue
        target = timestamp + horizon_seconds
        if target + reference_delay_seconds > decision_time:
            immature += 1
            continue
        initial = reference_asof(refs, timestamp, max_price_age_seconds, available_by=decision_time)
        final = reference_asof(refs, target, max_price_age_seconds, available_by=decision_time)
        if initial["value"] is None or final["value"] is None:
            missing += 1
            continue
        amount1 = Decimal(str(event.get("amount1", 0)))
        volume = abs(Decimal(str(event.get("amount0", 0)))) / Decimal(10) ** 6
        p0, p1 = Decimal(str(initial["value"])), Decimal(str(final["value"]))
        if not amount1 or not volume or p0 <= 0 or p1 <= 0:
            missing += 1
            continue
        sign = Decimal(1) if amount1 < 0 else Decimal(-1)
        numerator += volume * sign * (p1 / p0 - 1) * 10_000
        denominator += volume
        count += 1
    flags = []
    if not count:
        flags.append("NO_MATURE_MARKOUTS")
    if missing:
        flags.append("MARKOUT_REFERENCE_GAPS")
    return {"historical_markout_5m_bps": float(numerator / denominator) if denominator else None,
            "markout_mature_count": count, "markout_immature_count": immature,
            "markout_missing_reference_count": missing, "markout_flags": flags}


def build_features(decisions: Iterable[float], events: Iterable[Mapping[str, Any]],
                   references: Iterable[Mapping[str, Any]], states: Iterable[Mapping[str, Any]],
                   config: Mapping[str, Any]) -> list[dict]:
    settings = _settings(config)
    evs = apply_availability(events, settings["block_delay_seconds"])
    refs = asof_index(apply_availability(references, settings["reference_delay_seconds"]))
    pool = asof_index(apply_availability(states, settings["block_delay_seconds"]))
    result = []
    onchain_complete = bool(config.get("_onchain_coverage_complete", bool(evs or pool)))
    window = float(settings["lookback_seconds"])
    for decision in decisions:
        decision = float(decision)
        flags: list[str] = ["ASSUMED_BLOCK_AVAILABILITY", "ASSUMED_REFERENCE_AVAILABILITY", "PROVISIONAL_TOXICITY_PROXIES"]
        reference = reference_asof(refs, decision, settings["max_price_age_seconds"])
        initial_reference = reference_asof(refs, decision - window, settings["max_price_age_seconds"])
        flags.extend(reference["flags"])
        state = backward_asof(pool, decision, settings["max_pool_age_seconds"])
        if not onchain_complete:
            flags.append("ONCHAIN_UNAVAILABLE")
        if reference["row"] is None:
            flags.append("MISSING_OR_STALE_REFERENCE")
        if state["row"] is None:
            flags.append("MISSING_OR_STALE_POOL_STATE")
        visible_refs = sorted([r for r in refs if decision - window <= float(r["timestamp"]) <= decision
                               and float(r["available_at"]) <= decision], key=lambda r: float(r["timestamp"]))
        squared_returns = []
        reference_gap = False
        for left, right in zip(visible_refs, visible_refs[1:]):
            if float(right["timestamp"]) - float(left["timestamp"]) > settings["max_price_age_seconds"]:
                reference_gap = True
                continue
            p0, p1 = float(left["price_usdc_per_eth"]), float(right["price_usdc_per_eth"])
            if p0 > 0 and p1 > 0:
                squared_returns.append(log(p1 / p0) ** 2)
        # Require the full volatility window.
        coverage = (len(visible_refs) >= 2 and
                    float(visible_refs[0]["timestamp"]) <= decision - window + settings["max_price_age_seconds"] and
                    decision - float(visible_refs[-1]["timestamp"]) <= settings["max_price_age_seconds"])
        if reference_gap or not coverage:
            flags.append("INCOMPLETE_VOLATILITY_WINDOW")
        visible_swaps = [e for e in evs if e.get("event_type", "").lower() == "swap"
                         and decision - window < float(e["timestamp"]) <= decision
                         and float(e["available_at"]) <= decision]
        volume = sum((abs(Decimal(str(e.get("amount0", 0)))) / Decimal(10) ** 6 for e in visible_swaps), Decimal(0))
        signed_volume = sum(((Decimal(1) if Decimal(str(e.get("amount1", 0))) < 0 else Decimal(-1)) *
                             abs(Decimal(str(e.get("amount0", 0)))) / Decimal(10) ** 6
                             for e in visible_swaps if Decimal(str(e.get("amount1", 0))) != 0), Decimal(0))
        unclassified = any(Decimal(str(e.get("amount1", 0))) == 0 and Decimal(str(e.get("amount0", 0))) != 0
                           for e in visible_swaps)
        if unclassified:
            flags.append("UNCLASSIFIED_SWAP_DIRECTION")
        receipts = deduplicate_receipts([e for e in visible_swaps
                                        if e.get("gas_used") is not None and e.get("effective_gas_price") is not None])
        gas_prices = [float(e["effective_gas_price"]) / 1e9 for e in receipts]
        if not gas_prices:
            flags.append("GAS_RECEIPTS_UNAVAILABLE")
        elif config.get("_gas_receipts_sampled"):
            flags.append("GAS_RECEIPTS_SAMPLED_NOT_POPULATION_MEAN")
        price = float(reference["value"]) if reference["value"] is not None else None
        before = float(initial_reference["value"]) if initial_reference["value"] is not None else None
        dex_price = float(price_from_sqrt(int(state["row"]["sqrt_price_x96"]))) if state["row"] else None
        deviation = (dex_price / price - 1) * 10_000 if dex_price is not None and price and price > 0 else None
        markout = historical_markout(evs, refs, decision, lookback_seconds=window,
                                    horizon_seconds=settings["markout_seconds"],
                                    max_price_age_seconds=settings["max_price_age_seconds"],
                                    reference_delay_seconds=settings["reference_delay_seconds"])
        flags.extend(markout.pop("markout_flags"))
        result.append({"decision_time": decision, "feature_lookback_seconds": window,
                       "reference_price_usdc_per_eth": price, "reference_age_seconds": reference["age_seconds"],
                       "reference_eth_age_seconds": reference["eth_age_seconds"],
                       "reference_usdc_age_seconds": reference["usdc_age_seconds"],
                       "pool_price_usdc_per_eth": dex_price, "pool_age_seconds": state["age_seconds"],
                       "pool_observed_at": float(state["row"]["timestamp"]) if state["row"] else None,
                       "pool_available_at": float(state["row"]["available_at"]) if state["row"] else None,
                       "pool_state_changed_at": state["row"].get("state_changed_at", state["row"]["timestamp"]) if state["row"] else None,
                       "pool_last_event_at": state["row"].get("last_event_at", state["row"]["timestamp"]) if state["row"] else None,
                       "pool_block_number": state["row"].get("block_number") if state["row"] else None,
                       "pool_log_index": state["row"].get("log_index") if state["row"] else None,
                       "trailing_return": price / before - 1 if price and before and before > 0 else None,
                       "realized_volatility": sqrt(sum(squared_returns)) if squared_returns and coverage and not reference_gap else None,
                       "swap_volume_usdc": float(volume) if onchain_complete else None,
                       "swap_count": len(visible_swaps) if onchain_complete else None,
                       "active_liquidity": str(state["row"]["liquidity"]) if state["row"] else None,
                       "gas_price_mean_gwei": mean(gas_prices) if gas_prices else None,
                       "gas_transaction_count": len(receipts) if onchain_complete else None,
                       "dex_cex_deviation_bps": deviation,
                       "absolute_dex_cex_deviation_bps": abs(deviation) if deviation is not None else None,
                       "volume_imbalance": float(signed_volume / volume) if volume and onchain_complete and not unclassified else None,
                       **markout, "status": "BLOCKED" if price is None or dex_price is None or not onchain_complete else "WARN",
                       "flags": sorted(set(flags))})
    return result

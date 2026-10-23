"""Independent fixed-range LP episodes with a discrete left-inventory benchmark."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Mapping

from .costs import estimate_episode_costs
from .temporal import apply_availability, asof_index, backward_asof, benchmark_grid
from .uniswap import (liquidity_for_capital, position_amounts, price_from_sqrt,
                      tick_bounds_for_price)


def _amounts(state: Mapping[str, Any], lower: int, upper: int, liquidity: int, *,
             round_up: bool = False) -> tuple[Decimal, Decimal]:
    raw0, raw1 = position_amounts(int(state["sqrt_price_x96"]), lower, upper, liquidity, round_up)
    return Decimal(raw0) / Decimal(10) ** 6, Decimal(raw1) / Decimal(10) ** 18


def _in_range_fraction(states: list[dict], entry: float, exit: float, lower: int, upper: int,
                       initial: Mapping[str, Any]) -> float:
    ordered = sorted([row for row in states if entry < float(row["timestamp"]) < exit],
                     key=lambda row: (float(row["timestamp"]), int(row.get("block_number", 0)),
                                      int(row.get("transaction_index", 0)), int(row.get("log_index", -1))))
    left, tick, duration = entry, int(initial["tick"]), 0.0
    for state in ordered:
        at = float(state["timestamp"])
        duration += (at - left) * (lower <= tick < upper)
        left, tick = at, int(state["tick"])
    duration += (exit - left) * (lower <= tick < upper)
    return duration / (exit - entry)


def discrete_rebalancing_pnl(states: Iterable[Mapping[str, Any]], references: Iterable[Mapping[str, Any]],
                             entry: float, exit: float, lower_tick: int, upper_tick: int,
                             liquidity: int, *, step_seconds: int = 60,
                             max_price_age_seconds: float = 120,
                             max_pool_age_seconds: float = 120) -> dict:
    """Sum x_j * (P[j+1]-P[j]); principal WETH inventory is fixed on each interval.

    Inventory is evaluated using only the pool state with event time <= left
    grid endpoint. Fees remain outside this benchmark inventory.
    """
    pool, refs = asof_index(states), asof_index(references)
    grid = benchmark_grid(entry, exit, step_seconds)
    pnl = Decimal(0)
    for left, right in zip(grid, grid[1:]):
        inventory = backward_asof(pool, left, max_pool_age_seconds)
        p0 = backward_asof(refs, left, max_price_age_seconds, value_key="price_usdc_per_eth")
        p1 = backward_asof(refs, right, max_price_age_seconds, value_key="price_usdc_per_eth")
        if inventory["row"] is None or p0["value"] is None or p1["value"] is None:
            return {"pnl_usdc": None, "status": "BLOCKED", "flags": ["INCOMPLETE_REBALANCING_GRID"]}
        _, weth = _amounts(inventory["row"], lower_tick, upper_tick, liquidity)
        pnl += weth * (Decimal(str(p1["value"])) - Decimal(str(p0["value"])))
    return {"pnl_usdc": float(pnl), "grid_points": len(grid), "status": "WARN",
            "flags": ["DISCRETE_LEFT_INVENTORY_BENCHMARK", "FEE_WETH_EXCLUDED_FROM_BENCHMARK"]}


def evaluate_episodes(decisions: Iterable[float], events: Iterable[Mapping[str, Any]],
                      references: Iterable[Mapping[str, Any]], states: Iterable[Mapping[str, Any]],
                      segments: Iterable[Mapping[str, Any]], config: Mapping[str, Any],
                      cost_model: Mapping[str, Any] | None = None,
                      pool_observations: Iterable[Mapping[str, Any]] | None = None) -> list[dict]:
    """Evaluate each decision/width independently, keeping missing targets null.

    At entry all pool events timestamped <= entry are treated as already executed.
    Fee earning uses the open-entry/closed-exit interval, preventing a position
    from receiving fees from information used to decide or to fund its entry.
    """
    from .replay import marginal_fees

    timing = {"block_delay_seconds": 12, "reference_delay_seconds": 1,
              "execution_delay_seconds": 12, "max_price_age_seconds": 120,
              "max_pool_age_seconds": 120, "benchmark_seconds": 60, **config.get("timing", {})}
    pilot = {"holding_seconds": 3600, "capital_usdc": 10_000,
             "widths": [0.005, 0.01, 0.02], **config.get("pilot", {})}
    if timing["execution_delay_seconds"] < 0 or pilot["holding_seconds"] <= 0:
        raise ValueError("Execution delay must be nonnegative and horizon positive")
    spacing = int(config.get("pool", {}).get("tick_spacing", 10))
    threshold = Decimal(str(config.get("validation", {}).get("small_lp_max_share", 0.01)))
    capital = Decimal(str(pilot["capital_usdc"]))
    if capital <= 0:
        raise ValueError("Capital must be positive")
    refs = asof_index(apply_availability(references, timing["reference_delay_seconds"]))
    raw_states = list(states)
    observed_states = raw_states if pool_observations is None else list(pool_observations)
    decision_states = asof_index(apply_availability(observed_states, timing["block_delay_seconds"]))
    # Outcome inventories use completed event-time states; this is a retrospective
    # valuation convention, not an information set for choosing the range.
    valuation_states = [{**row, "available_at": float(row["timestamp"])} for row in raw_states]
    valuation_index = asof_index([{**row, "available_at": float(row["timestamp"])} for row in observed_states])
    evs, paths = list(events), list(segments)
    model = cost_model if cost_model is not None else config.get("costs")
    result: list[dict] = []
    for decision in decisions:
        decision = float(decision)
        entry = decision + float(timing["execution_delay_seconds"])
        exit = entry + float(pilot["holding_seconds"])
        known = backward_asof(decision_states, decision, timing["max_pool_age_seconds"])
        start_state = backward_asof(valuation_index, entry, timing["max_pool_age_seconds"])
        end_state = backward_asof(valuation_index, exit, timing["max_pool_age_seconds"])
        p0 = backward_asof(refs, entry, timing["max_price_age_seconds"], value_key="price_usdc_per_eth")
        p1 = backward_asof(refs, exit, timing["max_price_age_seconds"], value_key="price_usdc_per_eth")
        for width in pilot["widths"]:
            row: dict = {"decision_time": decision, "entry_time": entry, "exit_time": exit,
                         "holding_seconds": float(pilot["holding_seconds"]), "width": float(width),
                         "capital_usdc": float(capital), "status": "BLOCKED", "flags": [],
                         "y_usdc": None, "net_lp_pnl_usdc": None,
                         "sensitivity_y_usdc": None, "sensitivity_net_lp_pnl_usdc": None,
                         "fees0_usdc": None, "fees1_weth": None, "fees_end_value_usdc": None}
            missing = [name for name, check in (("DECISION_POOL_STATE", known), ("ENTRY_POOL_STATE", start_state),
                                                ("EXIT_POOL_STATE", end_state), ("ENTRY_REFERENCE", p0),
                                                ("EXIT_REFERENCE", p1)) if check["row"] is None]
            if missing:
                row["flags"] = [f"MISSING_OR_STALE_{name}" for name in missing]
                result.append(row)
                continue
            if Decimal(str(p0["value"])) <= 0 or Decimal(str(p1["value"])) <= 0:
                row["flags"] = ["INVALID_REFERENCE_PRICE"]
                result.append(row)
                continue
            center = price_from_sqrt(int(known["row"]["sqrt_price_x96"]))
            lower, upper = tick_bounds_for_price(center, Decimal(str(width)), spacing)
            liquidity = liquidity_for_capital(capital, Decimal(str(p0["value"])),
                                              int(start_state["row"]["sqrt_price_x96"]), lower, upper)
            start0, start1 = _amounts(start_state["row"], lower, upper, liquidity, round_up=True)
            end0, end1 = _amounts(end_state["row"], lower, upper, liquidity)
            ref0, ref1 = Decimal(str(p0["value"])), Decimal(str(p1["value"]))
            start_value, end_value = start0 + start1 * ref0, end0 + end1 * ref1
            cash_residual = capital - start_value
            hold_end_value = start0 + start1 * ref1
            episode_segments = [s for s in paths if entry < float(s["timestamp"]) <= exit]
            episode_swaps = [e for e in evs if e.get("event_type", "").lower() in ("swap", "flash")
                             and entry < float(e["timestamp"]) <= exit]
            fees = marginal_fees(episode_segments, lower, upper, liquidity)
            flags = ["INDEPENDENT_EPISODE", "RANGE_CENTERED_AT_DECISION", "MARGINAL_FIXED_HISTORICAL_PATH",
                     "BLOCK_TIMESTAMP_BOUNDARY_CONVENTION", "ASSUMED_EXECUTION_DELAY", "RETAINED_FEE_WETH_EXPOSURE",
                     "RESIDUAL_CASH_RETAINED", *fees.get("flags", [])]
            # A missing path for an observed swap cannot mean zero earned fees.
            segment_keys = {(int(s["block_number"]), int(s.get("transaction_index", 0)), int(s.get("log_index", -1)))
                            for s in episode_segments}
            swap_keys = {(int(s["block_number"]), int(s.get("transaction_index", 0)), int(s.get("log_index", -1)))
                         for s in episode_swaps}
            fee_missing = bool(swap_keys - segment_keys) or fees.get("status") in ("BLOCKED", "FAIL")
            if swap_keys - segment_keys:
                flags.append("MISSING_SWAP_OR_FLASH_FEE_SEGMENTS")
            fees0_raw, fees1_raw = fees.get("fees0_raw"), fees.get("fees1_raw")
            if fees0_raw is None or fees1_raw is None:
                fee_missing = True
            fee0 = None if fee_missing else Decimal(str(fees0_raw)) / Decimal(10) ** 6
            fee1 = None if fee_missing else Decimal(str(fees1_raw)) / Decimal(10) ** 18
            fee_value = fee0 + fee1 * ref1 if fee0 is not None and fee1 is not None else None
            share_value = fees.get("max_liquidity_share")
            max_share = Decimal(str(share_value)) if share_value is not None else Decimal(0)
            # Mint/Burn changes can lower historical liquidity without any swap.
            # Include every interval state, not only fee-generating segments.
            relevant_states = [start_state["row"]] + [state for state in valuation_states
                                if entry < float(state["timestamp"]) <= exit]
            for path_state in relevant_states:
                if lower <= int(path_state["tick"]) < upper:
                    hist_liquidity = int(path_state["liquidity"])
                    if hist_liquidity <= 0:
                        flags.append("ZERO_HISTORICAL_ACTIVE_LIQUIDITY")
                        fee_missing = True
                    else:
                        max_share = max(max_share, Decimal(liquidity) / Decimal(hist_liquidity))
            if fee_missing:
                fee0 = fee1 = fee_value = None
            if max_share > threshold:
                flags.append("SMALL_LP_ASSUMPTION_VIOLATED")
            rb = discrete_rebalancing_pnl(valuation_index, refs, entry, exit, lower, upper, liquidity,
                                          step_seconds=int(timing["benchmark_seconds"]),
                                          max_price_age_seconds=timing["max_price_age_seconds"],
                                          max_pool_age_seconds=timing["max_pool_age_seconds"])
            flags.extend(rb["flags"])
            costs = estimate_episode_costs(model, capital, ref0, ref1)
            flags.extend(costs["flags"])
            gross = end_value + fee_value - start_value if fee_value is not None and not fee_missing else None
            empirical_cost = costs["empirical_total_usdc"]
            sensitivity_cost = costs["sensitivity_total_usdc"]
            net = float(gross) - empirical_cost if gross is not None and empirical_cost is not None else None
            sensitivity_net = float(gross) - sensitivity_cost if gross is not None and sensitivity_cost is not None else None
            rb_pnl = rb["pnl_usdc"]
            blocked = fee_missing or rb_pnl is None or costs["status"] == "BLOCKED"
            row.update({"range_center_usdc_per_eth": float(center), "decision_pool_block_number": known["row"].get("block_number"),
                        "decision_pool_event_time": known["row"].get("last_event_at", float(known["row"]["timestamp"])),
                        "decision_pool_observed_at": float(known["row"]["timestamp"]),
                        "decision_pool_available_at": float(known["row"]["available_at"]),
                        "decision_pool_state_changed_at": known["row"].get("state_changed_at", float(known["row"]["timestamp"])),
                        "entry_pool_observed_at": float(start_state["row"]["timestamp"]),
                        "exit_pool_observed_at": float(end_state["row"]["timestamp"]),
                        "lower_tick": lower, "upper_tick": upper, "position_liquidity": str(liquidity),
                        "entry_pool_price_usdc_per_eth": float(price_from_sqrt(int(start_state["row"]["sqrt_price_x96"]))),
                        "start_token0_usdc": float(start0), "start_token1_weth": float(start1),
                        "end_token0_usdc": float(end0), "end_token1_weth": float(end1),
                        "cash_residual_usdc": float(cash_residual),
                        "start_value_ex_fees_usdc": float(start_value), "end_value_ex_fees_usdc": float(end_value),
                        "fees0_usdc": float(fee0) if fee0 is not None else None,
                        "fees1_weth": float(fee1) if fee1 is not None else None,
                        "fees_end_value_usdc": float(fee_value) if fee_value is not None else None,
                        "fee_status": "BLOCKED" if fee_missing else fees.get("status", "WARN"),
                        "gross_lp_pnl_usdc": float(gross) if gross is not None else None,
                        "net_lp_pnl_usdc": net,
                        "hold_initial_tokens_pnl_usdc": float(hold_end_value - start_value),
                        "pnl_vs_hold_usdc": net - float(hold_end_value - start_value) if net is not None else None,
                        "rebalancing_pnl_usdc": rb_pnl,
                        "y_usdc": net - rb_pnl if net is not None and rb_pnl is not None else None,
                        "sensitivity_net_lp_pnl_usdc": sensitivity_net,
                        "sensitivity_pnl_vs_hold_usdc": sensitivity_net - float(hold_end_value - start_value) if sensitivity_net is not None else None,
                        "sensitivity_y_usdc": sensitivity_net - rb_pnl if sensitivity_net is not None and rb_pnl is not None else None,
                        "cost_model_kind": costs["model_kind"], **{f"cost_{key}": value for key, value in costs.items() if key.endswith("usdc")},
                        "time_weighted_in_range": _in_range_fraction(valuation_states, entry, exit, lower, upper, start_state["row"]),
                        "max_liquidity_share": float(max_share), "max_liquidity_share_threshold": float(threshold),
                        "status": "BLOCKED" if blocked else "WARN", "flags": sorted(set(flags))})
            result.append(row)
    widths = sorted(float(w) for w in pilot["widths"])
    middle = widths[len(widths) // 2]
    for row in result:
        baseline = next((r for r in result if r["decision_time"] == row["decision_time"] and r["width"] == middle), None)
        row["middle_width_benchmark"] = middle
        for key in ("y_usdc", "sensitivity_y_usdc"):
            row[f"{key}_vs_middle"] = row.get(key) - baseline[key] if baseline and row.get(key) is not None and baseline.get(key) is not None else None
    return result

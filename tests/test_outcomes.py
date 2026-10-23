"""Synthetic episodes for timing and accounting checks."""
from copy import deepcopy
from decimal import Decimal

import pytest

from lp_pilot.outcomes import discrete_rebalancing_pnl, evaluate_episodes
from lp_pilot.uniswap import get_tick_at_sqrt_ratio, position_amounts, sqrt_from_price, tick_bounds_for_price


def state(timestamp, price="2500"):
    sqrt = sqrt_from_price(price)
    return {"timestamp": timestamp, "block_number": int(timestamp), "transaction_index": 0, "log_index": 0,
            "sqrt_price_x96": sqrt, "tick": get_tick_at_sqrt_ratio(sqrt), "liquidity": 10**22}


def configuration():
    return {"pilot": {"holding_seconds": 60, "capital_usdc": 10000, "widths": [.005, .01, .02]},
            "timing": {"block_delay_seconds": 0, "reference_delay_seconds": 0,
                       "execution_delay_seconds": 5, "benchmark_seconds": 30,
                       "max_pool_age_seconds": 120, "max_price_age_seconds": 120},
            "pool": {"tick_spacing": 10},
            "costs": {"mode": "sensitivity", "mint_gas": 250000, "burn_gas": 150000,
                      "collect_gas": 100000, "gas_price_gwei": 30, "token_conversion_bps": 5}}


def test_three_independent_ranges_sensitivity_and_determinism():
    states = [state(0), state(30), state(65)]
    refs = [{"timestamp": t, "price_usdc_per_eth": 2500} for t in (0, 30, 65)]
    args = ([0], [], refs, states, [], configuration())
    rows = evaluate_episodes(*args)
    assert rows == evaluate_episodes(*args)
    assert len(rows) == 3
    for row in rows:
        assert row["capital_usdc"] == 10000
        assert row["net_lp_pnl_usdc"] is None
        assert row["y_usdc"] is None
        assert row["sensitivity_y_usdc"] == pytest.approx(-42.5, abs=2e-6)
        assert row["time_weighted_in_range"] == 1
        assert row["fees0_usdc"] == 0
        assert row["sensitivity_y_usdc_vs_middle"] == pytest.approx(0, abs=2e-6)
        assert row["status"] == "WARN"


def test_no_future_recentering_and_no_fees_from_decision_events():
    states = [state(0), state(5, "2600"), state(65, "2600")]
    refs = [{"timestamp": t, "price_usdc_per_eth": 2600} for t in (0, 5, 35, 65)]
    sqrt = sqrt_from_price("2500")
    segment = {"timestamp": 0, "block_number": 0, "transaction_index": 0, "log_index": 0,
               "sqrt_start_x96": sqrt, "sqrt_end_x96": sqrt + 1, "liquidity": 10**22,
               "fee_token": 0, "fee_amount_lp": 1000000, "protocol_fee": 0,
               "exactness": "reference_exact", "zero_for_one": False}
    rows = evaluate_episodes([0], [], refs, states, [segment], configuration())
    for row in rows:
        assert row["range_center_usdc_per_eth"] == pytest.approx(2500)
        assert row["entry_pool_price_usdc_per_eth"] == pytest.approx(2600)
        assert row["fees0_usdc"] == 0
        assert row["lower_tick"] % 10 == row["upper_tick"] % 10 == 0


def test_missing_fee_path_or_costs_cannot_become_zero_outcomes():
    states = [state(0), state(30), state(65)]
    refs = [{"timestamp": t, "price_usdc_per_eth": 2500} for t in (0, 30, 65)]
    swaps = [{"timestamp": 30, "block_number": 30, "transaction_index": 0, "log_index": 0, "event_type": "swap"}]
    row = evaluate_episodes([0], swaps, refs, states, [], configuration())[0]
    assert row["fees0_usdc"] is None
    assert row["gross_lp_pnl_usdc"] is None
    assert row["status"] == "BLOCKED"
    config = configuration()
    config["costs"] = {}
    row = evaluate_episodes([0], [], refs, states, [], config)[0]
    assert row["sensitivity_y_usdc"] is None
    assert row["status"] == "BLOCKED"


def test_rebalancing_uses_left_endpoint_inventory_only():
    lower, upper = tick_bounds_for_price("2500", ".01", 10)
    states = [state(0), state(30, "2600"), state(60, "2600")]
    refs = [{"timestamp": 0, "price_usdc_per_eth": 2500},
            {"timestamp": 30, "price_usdc_per_eth": 2600},
            {"timestamp": 60, "price_usdc_per_eth": 2600}]
    liquidity = 10**12
    result = discrete_rebalancing_pnl(states, refs, 0, 60, lower, upper, liquidity, step_seconds=30)
    initial_weth = Decimal(position_amounts(states[0]["sqrt_price_x96"], lower, upper, liquidity)[1]) / Decimal(10)**18
    assert result["pnl_usdc"] == pytest.approx(float(initial_weth * 100))
    refs[1]["available_at"] = 100
    # The delayed quote is unavailable; an older quote remains usable only while fresh.
    assert discrete_rebalancing_pnl(states, refs, 0, 60, lower, upper, liquidity,
                                    step_seconds=30, max_price_age_seconds=10)["pnl_usdc"] is None


def test_small_lp_share_checks_mint_burn_states_without_any_swaps():
    states = [state(0), {**state(30), "liquidity": 1}, state(65)]
    refs = [{"timestamp": t, "price_usdc_per_eth": 2500} for t in (0, 30, 65)]
    row = evaluate_episodes([0], [], refs, states, [], configuration())[0]
    assert "SMALL_LP_ASSUMPTION_VIOLATED" in row["flags"]
    assert row["max_liquidity_share"] > row["max_liquidity_share_threshold"]
    states[1]["liquidity"] = 0
    row = evaluate_episodes([0], [], refs, states, [], configuration())[0]
    assert "ZERO_HISTORICAL_ACTIVE_LIQUIDITY" in row["flags"]
    assert row["fees0_usdc"] is None
    assert row["sensitivity_y_usdc"] is None
    assert row["status"] == "BLOCKED"


def test_flash_without_fee_evidence_blocks_instead_of_assuming_zero():
    states = [state(0), state(30), state(65)]
    refs = [{"timestamp": t, "price_usdc_per_eth": 2500} for t in (0, 30, 65)]
    flash = {"event_type": "Flash", "timestamp": 30, "block_number": 30,
             "transaction_index": 0, "log_index": 0}
    row = evaluate_episodes([0], [flash], refs, states, [], configuration())[0]
    assert row["status"] == "BLOCKED"
    assert row["fees_end_value_usdc"] is None


def test_mint_deposits_round_up_and_burn_principal_rounds_down():
    states = [state(0), state(30), state(65)]
    refs = [{"timestamp": t, "price_usdc_per_eth": 2500} for t in (0, 30, 65)]
    row = evaluate_episodes([0], [], refs, states, [], configuration())[0]
    raw0, raw1 = position_amounts(states[0]["sqrt_price_x96"], row["lower_tick"], row["upper_tick"],
                                 int(row["position_liquidity"]), True)
    assert row["start_token0_usdc"] == float(Decimal(raw0) / Decimal(10)**6)
    assert row["start_token1_weth"] == float(Decimal(raw1) / Decimal(10)**18)
    assert row["gross_lp_pnl_usdc"] <= 0
    assert row["start_value_ex_fees_usdc"] + row["cash_residual_usdc"] == pytest.approx(10000, abs=1e-9)


def test_marginal_fee_block_does_not_crash_or_yield_pnl():
    states = [state(0), state(30), state(65)]
    refs = [{"timestamp": t, "price_usdc_per_eth": 2500} for t in (0, 30, 65)]
    sqrt = states[0]["sqrt_price_x96"]
    segment = {"timestamp": 30, "block_number": 30, "transaction_index": 0, "log_index": 0,
               "sqrt_start_x96": sqrt, "sqrt_end_x96": sqrt, "tick_start": states[0]["tick"],
               "liquidity": 0, "fee_token": 0, "fee_amount_lp": 10, "exactness": "reference_exact"}
    row = evaluate_episodes([0], [], refs, states, [segment], configuration())[0]
    assert row["status"] == "BLOCKED"
    assert row["sensitivity_y_usdc"] is None

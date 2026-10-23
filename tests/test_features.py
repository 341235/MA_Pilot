"""Synthetic data for feature timing and leakage checks."""
from copy import deepcopy

import pytest

from lp_pilot.features import build_features, historical_markout
from lp_pilot.uniswap import sqrt_from_price


def swap(timestamp, amount0=100_000_000, amount1=-10**18, **extra):
    return {"timestamp": timestamp, "event_type": "swap", "amount0": amount0,
            "amount1": amount1, "transaction_hash": f"0x{timestamp}", **extra}


def test_markout_is_excluded_until_horizon_and_data_delay_mature():
    events = [swap(100)]
    refs = [{"timestamp": 100, "available_at": 101, "price_usdc_per_eth": 2500},
            {"timestamp": 400, "available_at": 401, "price_usdc_per_eth": 2525}]
    early = historical_markout(events, refs, 400)
    mature = historical_markout(events, refs, 401)
    assert early["historical_markout_5m_bps"] is None
    assert early["markout_immature_count"] == 1
    assert mature["historical_markout_5m_bps"] == pytest.approx(100)
    assert mature["markout_mature_count"] == 1


def test_markout_sign_volume_weight_and_missing_horizon():
    events = [swap(100), swap(100, amount0=300_000_000, amount1=10**18)]
    refs = [{"timestamp": 100, "price_usdc_per_eth": 100},
            {"timestamp": 400, "price_usdc_per_eth": 101}]
    result = historical_markout(events, refs, 401)
    assert result["historical_markout_5m_bps"] == pytest.approx(-50)
    assert historical_markout(events, refs[:1], 401, max_price_age_seconds=120)["historical_markout_5m_bps"] is None


def test_features_are_invariant_to_future_and_unavailable_inputs():
    refs = [{"timestamp": t, "price_usdc_per_eth": 2500 + t / 60} for t in range(0, 601, 60)]
    states = [{"timestamp": 540, "sqrt_price_x96": sqrt_from_price("2500"), "liquidity": 10**18}]
    events = [swap(100), swap(200, amount1=10**18), swap(590)]
    config = {"timing": {"lookback_seconds": 540, "block_delay_seconds": 12, "reference_delay_seconds": 1}}
    baseline = build_features([600], events, refs, states, config)
    poisoned_refs = refs + [{"timestamp": 601, "available_at": 601, "price_usdc_per_eth": 999999},
                            {"timestamp": 599, "available_at": 700, "price_usdc_per_eth": 1}]
    poisoned_events = events + [swap(601), swap(599, available_at=700)]
    poisoned_states = states + [{"timestamp": 599, "available_at": 700,
                                 "sqrt_price_x96": sqrt_from_price("1"), "liquidity": 1}]
    assert build_features([600], poisoned_events, poisoned_refs, poisoned_states, config) == baseline
    assert baseline[0]["swap_count"] == 2
    assert baseline[0]["swap_volume_usdc"] == 200
    assert baseline[0]["volume_imbalance"] == 0
    assert baseline[0]["reference_price_usdc_per_eth"] == 2509


def test_gas_transaction_information_is_not_counted_per_swap_event():
    tx = {"transaction_hash": "0xsame", "gas_used": 100000, "effective_gas_price": 20_000_000_000}
    events = [swap(100, **tx), swap(101, **tx)]
    config = {"timing": {"block_delay_seconds": 0, "reference_delay_seconds": 0}}
    row = build_features([150], events, [{"timestamp": 150, "price_usdc_per_eth": 2500}],
                         [{"timestamp": 150, "sqrt_price_x96": sqrt_from_price("2500"), "liquidity": 10**18}], config)[0]
    assert row["gas_transaction_count"] == 1
    assert row["gas_price_mean_gwei"] == 20


def test_absent_onchain_data_is_missing_instead_of_observed_zero():
    row = build_features([600], [], [{"timestamp": 600, "price_usdc_per_eth": 2500}], [],
                         {"timing": {"reference_delay_seconds": 0}})[0]
    assert row["swap_count"] is None
    assert row["swap_volume_usdc"] is None
    assert row["volume_imbalance"] is None
    assert "ONCHAIN_UNAVAILABLE" in row["flags"]
    # Verified complete extraction may genuinely contain no swaps.
    row = build_features([600], [], [{"timestamp": 600, "price_usdc_per_eth": 2500}], [],
                         {"timing": {"reference_delay_seconds": 0}, "_onchain_coverage_complete": True})[0]
    assert row["swap_count"] == 0
    assert row["swap_volume_usdc"] == 0

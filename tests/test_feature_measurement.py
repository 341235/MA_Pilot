"""Feature units, historical endpoints and information available at decisions."""
from copy import deepcopy

import pytest

from lp_pilot.features import build_features, historical_markout
from lp_pilot.uniswap import sqrt_from_price


def swap(timestamp, amount0=100_000_000, amount1=-10**18, **extra):
    return {"timestamp": timestamp, "event_type": "Swap", "amount0": amount0,
            "amount1": amount1, "transaction_hash": f"0x{timestamp}", **extra}


def observation(timestamp=588, **extra):
    return {"timestamp": timestamp, "observed_at": timestamp,
            "sqrt_price_x96": sqrt_from_price("100"), "liquidity": 10**18,
            "state_changed_at": 300, "last_event_at": 300,
            "block_number": 41, "event_type": "BlockObservation", **extra}


def references():
    return [{"timestamp": t, "price_usdc_per_eth": 100 + t / 6000}
            for t in range(0, 601, 60)]


def config(**timing):
    return {"timing": {"lookback_seconds": 600, "block_delay_seconds": 12,
                       "reference_delay_seconds": 1, **timing},
            "_onchain_coverage_complete": True}


def test_markout_final_quote_is_anchored_to_the_horizon_not_the_decision():
    refs = [{"timestamp": 100, "available_at": 101, "price_usdc_per_eth": 100},
            {"timestamp": 400, "available_at": 401, "price_usdc_per_eth": 101},
            {"timestamp": 450, "available_at": 451, "price_usdc_per_eth": 200}]
    result = historical_markout([swap(100)], refs, 500)
    assert result["historical_markout_5m_bps"] == pytest.approx(100)
    assert result["markout_mature_count"] == 1
    refs[-1]["price_usdc_per_eth"] = 1
    assert historical_markout([swap(100)], refs, 500) == result


def test_later_available_quote_cannot_fill_a_missing_horizon_endpoint():
    refs = [{"timestamp": 100, "price_usdc_per_eth": 100},
            {"timestamp": 450, "price_usdc_per_eth": 101}]
    result = historical_markout([swap(100)], refs, 500)
    assert result["historical_markout_5m_bps"] is None
    assert result["markout_missing_reference_count"] == 1
    assert "MARKOUT_REFERENCE_GAPS" in result["markout_flags"]


def test_markout_requires_both_horizon_maturity_and_actual_endpoint_availability():
    refs = [{"timestamp": 100, "available_at": 101, "price_usdc_per_eth": 100},
            {"timestamp": 400, "available_at": 420, "price_usdc_per_eth": 101}]
    assert historical_markout([swap(100)], refs, 400.999)["markout_immature_count"] == 1
    late = historical_markout([swap(100)], refs, 401)
    assert late["historical_markout_5m_bps"] is None
    assert late["markout_missing_reference_count"] == 1
    assert historical_markout([swap(100)], refs, 420)["historical_markout_5m_bps"] == pytest.approx(100)


def test_a_candle_close_is_excluded_until_its_close_and_reception_delay():
    refs = [{"timestamp": t + 60, "available_at": t + 61, "price_usdc_per_eth": 100}
            for t in (300, 360, 420, 480)]
    unfinished = {"timestamp": 600, "available_at": 601, "price_usdc_per_eth": 200}
    settings = config(lookback_seconds=240)
    baseline = build_features([600], [], refs, [observation()], settings)[0]
    row = build_features([600], [], [*refs, unfinished], [observation()], settings)[0]
    assert row == baseline
    assert row["reference_price_usdc_per_eth"] == 100
    row = build_features([601], [], [*refs, unfinished], [observation()], settings)[0]
    assert row["reference_price_usdc_per_eth"] == 200


def test_eth_taker_sign_quote_units_and_volume_weighted_markout():
    events = [swap(100), swap(100, amount0=-300_000_000, amount1=3 * 10**18,
                              transaction_hash="0xsell")]
    refs = [{"timestamp": 100, "price_usdc_per_eth": 100},
            {"timestamp": 400, "price_usdc_per_eth": 101}]
    row = build_features([401], events, refs, [observation(389)], config())[0]
    assert row["swap_count"] == 2
    assert row["swap_volume_usdc"] == 400
    assert row["volume_imbalance"] == pytest.approx(-0.5)
    assert row["historical_markout_5m_bps"] == pytest.approx(-50)


def test_positive_quote_volume_without_weth_direction_does_not_become_a_sell():
    events = [swap(100), swap(101, amount0=300_000_000, amount1=0)]
    row = build_features([600], events, references(), [observation()], config())[0]
    assert row["swap_count"] == 2
    assert row["swap_volume_usdc"] == 400
    assert row["volume_imbalance"] is None
    assert "UNCLASSIFIED_SWAP_DIRECTION" in row["flags"]
    assert row["markout_mature_count"] == 1
    assert row["markout_missing_reference_count"] == 1


def test_confirmed_quiet_observations_do_not_create_trades_or_zero_valued_markouts():
    states = [observation(t) for t in (540, 552, 564, 576, 588)]
    row = build_features([600], [], references(), states, config())[0]
    assert row["swap_count"] == 0
    assert row["swap_volume_usdc"] == 0
    assert row["volume_imbalance"] is None
    assert row["historical_markout_5m_bps"] is None
    assert row["markout_mature_count"] == 0
    assert row["pool_state_changed_at"] == 300
    assert row["pool_age_seconds"] == 12
    assert "NO_MATURE_MARKOUTS" in row["flags"]


def test_trade_window_is_open_on_the_left_and_respects_block_reception():
    events = [swap(0), swap(1), swap(588), swap(589), swap(600)]
    row = build_features([600], events, references(), [observation()], config())[0]
    assert row["swap_count"] == 2  # t=0 excluded; t=589 and t=600 not received.
    assert row["swap_volume_usdc"] == 200


def test_past_decision_features_do_not_change_when_future_available_inputs_change():
    events = [swap(100), swap(200, amount0=-300_000_000, amount1=10**18)]
    refs, states = references(), [observation()]
    row = build_features([600], events, refs, states, config())
    poisoned_events = events + [swap(150, amount0=10**15, available_at=700), swap(700)]
    poisoned_refs = refs + [{"timestamp": 400, "available_at": 700, "price_usdc_per_eth": 1},
                            {"timestamp": 700, "price_usdc_per_eth": 10**9}]
    poisoned_states = states + [observation(599, available_at=700, liquidity=1), observation(700)]
    assert build_features([600], poisoned_events, poisoned_refs, poisoned_states, config()) == row
    changed = deepcopy(poisoned_refs)
    changed[-2]["price_usdc_per_eth"] = 10**12
    assert build_features([600], poisoned_events, changed, poisoned_states, config()) == row


def test_gas_sample_deduplicates_transactions_and_keeps_the_sampling_warning():
    gas = {"gas_used": 250_000, "effective_gas_price": 20_000_000_000,
           "transaction_hash": "0xsame"}
    events = [swap(100, **gas), swap(101, **gas)]
    settings = {**config(), "_gas_receipts_sampled": True}
    row = build_features([600], events, references(), [observation()], settings)[0]
    assert row["gas_transaction_count"] == 1
    assert row["gas_price_mean_gwei"] == 20
    assert "GAS_RECEIPTS_SAMPLED_NOT_POPULATION_MEAN" in row["flags"]


@pytest.mark.parametrize("refs", [[], [{"timestamp": 300, "price_usdc_per_eth": 100}]])
def test_missing_or_stale_reference_remains_null_without_invalidating_observed_trade_counts(refs):
    row = build_features([600], [swap(100)], refs, [observation()], config())[0]
    assert row["reference_price_usdc_per_eth"] is None
    assert row["dex_cex_deviation_bps"] is None
    assert row["historical_markout_5m_bps"] is None
    assert row["swap_count"] == 1
    assert row["swap_volume_usdc"] == 100
    assert "MISSING_OR_STALE_REFERENCE" in row["flags"]

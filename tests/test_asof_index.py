"""Indexed join ordering, availability and output parity."""
import random

import pytest

import lp_pilot.features as features
import lp_pilot.outcomes as outcomes
from lp_pilot.temporal import AsOfIndex, asof_index, backward_asof
from lp_pilot.uniswap import get_tick_at_sqrt_ratio, sqrt_from_price, tick_bounds_for_price


@pytest.mark.parametrize("seed", [5, 17, 42])
def test_index_matches_iterable_queries_with_duplicate_keys_and_late_arrivals(seed):
    rng = random.Random(seed)
    rows = []
    for ordinal in range(500):
        timestamp = rng.randrange(60)
        row = {"timestamp": timestamp, "available_at": timestamp + rng.randrange(40),
               "block_number": rng.choice([None, 0, 1, 2]),
               "transaction_index": rng.choice([None, 0, 1]),
               "log_index": rng.choice([None, -1, 0, 1]), "value": ordinal}
        if rng.randrange(5) == 0:
            row.pop("available_at")
        rows.append(row)
    rng.shuffle(rows)
    index = asof_index(iter(rows))
    assert list(index) == rows
    assert asof_index(index) is index
    for _ in range(200):
        at = rng.randrange(-5, 105) + rng.choice([0, 0.5])
        known_at = rng.choice([None, at, rng.randrange(100)])
        age = rng.randrange(30)
        assert backward_asof(index, at, age, value_key="value", available_by=known_at) == backward_asof(
            rows, at, age, value_key="value", available_by=known_at)


def test_equal_ordering_keys_keep_first_eligible_input_row():
    rows = [{"timestamp": 10, "available_at": 20, "value": "first"},
            {"timestamp": 10, "available_at": 11, "value": "second"},
            {"timestamp": 10, "available_at": 10, "value": "third"}]
    index = asof_index(rows)
    for known_at, expected in [(10, "third"), (11, "second"), (20, "first")]:
        assert backward_asof(index, 10, 10, value_key="value", available_by=known_at)["value"] == expected


def test_newest_event_time_wins_over_newest_arrival_and_stale_candidate_is_identical():
    rows = [{"timestamp": 4, "available_at": 20, "value": "late old quote"},
            {"timestamp": 8, "available_at": 9, "value": "newer quote"},
            {"timestamp": 10, "available_at": 100, "value": "unavailable quote"},
            {"timestamp": 21, "value": "future quote"}]
    index = asof_index(rows)
    assert backward_asof(index, 20, 20, value_key="value")["value"] == "newer quote"
    stale = backward_asof(index, 20, 5, value_key="value")
    assert stale == backward_asof(rows, 20, 5, value_key="value")
    assert stale["candidate"]["value"] == "newer quote"
    assert backward_asof(index, 10, 1, value_key="value", available_by=100)["value"] == "unavailable quote"


def test_empty_and_missing_join_and_invalid_age_preserve_results():
    for rows in ([], [{"timestamp": 100, "value": 1}]):
        assert backward_asof(asof_index(rows), 10, 1) == backward_asof(rows, 10, 1)
    with pytest.raises(ValueError, match="nonnegative"):
        backward_asof(asof_index([]), 10, -1)


def state(timestamp):
    sqrt = sqrt_from_price(str(2500 + timestamp / 300))
    return {"timestamp": timestamp, "block_number": timestamp, "transaction_index": 0, "log_index": 0,
            "sqrt_price_x96": sqrt, "tick": get_tick_at_sqrt_ratio(sqrt), "liquidity": 10**22}


class NoIterationIndex(AsOfIndex):
    def __iter__(self):
        raise AssertionError("An existing query index must be reused without materializing its rows")


def test_markout_and_rebalancing_reuse_existing_indices():
    refs = NoIterationIndex([{"timestamp": t, "price_usdc_per_eth": 2500 + t / 300} for t in range(0, 301, 30)])
    swaps = [{"timestamp": 30, "event_type": "Swap", "amount0": 100_000_000, "amount1": -10**18}]
    result = features.historical_markout(swaps, refs, 100, horizon_seconds=30)
    assert result["markout_mature_count"] == 1
    pool = NoIterationIndex([state(t) for t in range(0, 301, 30)])
    lower, upper = tick_bounds_for_price("2500", ".01", 10)
    result = outcomes.discrete_rebalancing_pnl(pool, refs, 0, 60, lower, upper, 10**12, step_seconds=30)
    assert result["pnl_usdc"] is not None


def test_feature_and_episode_outputs_match_plain_iterable_execution(monkeypatch):
    refs = [{"timestamp": t, "price_usdc_per_eth": 2500 + t / 300} for t in range(0, 301, 30)]
    refs.append({"timestamp": 210, "available_at": 500, "price_usdc_per_eth": 1})
    states = [state(t) for t in range(0, 301, 30)]
    swaps = [{"timestamp": t, "event_type": "Swap", "amount0": 100_000_000,
              "amount1": -10**18, "transaction_hash": str(t)} for t in (30, 120)]
    config = {"timing": {"lookback_seconds": 120, "markout_seconds": 30,
                         "block_delay_seconds": 0, "reference_delay_seconds": 0,
                         "execution_delay_seconds": 5, "benchmark_seconds": 30},
              "pilot": {"holding_seconds": 60, "capital_usdc": 10000, "widths": [.005, .01, .02]},
              "costs": {"mode": "sensitivity", "mint_gas": 250000, "burn_gas": 150000,
                        "collect_gas": 100000, "gas_price_gwei": 30, "token_conversion_bps": 5}}
    indexed_features = features.build_features([150, 240], swaps, refs, states, config)
    indexed_outcomes = outcomes.evaluate_episodes([30, 150], [], refs, states, [], config)
    monkeypatch.setattr(features, "asof_index", lambda rows: rows)
    monkeypatch.setattr(outcomes, "asof_index", lambda rows: rows)
    assert features.build_features([150, 240], swaps, refs, states, config) == indexed_features
    assert outcomes.evaluate_episodes([30, 150], [], refs, states, [], config) == indexed_outcomes

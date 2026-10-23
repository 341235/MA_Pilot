"""A fresh ETH quote cannot hide an old or unavailable conversion price."""

from copy import deepcopy

import pytest

from lp_pilot.features import build_features
from lp_pilot.fine_reference import convert_aggtrades
from lp_pilot.temporal import asof_index, backward_asof, reference_asof
from lp_pilot.uniswap import sqrt_from_price


def converted(**changes):
    return {
        "timestamp": 1000, "available_at": 1001, "price_usdc_per_eth": 2500,
        "eth_timestamp": 1000, "usdc_timestamp": 1000,
        "eth_available_at": 1001, "usdc_available_at": 1001,
        "sequence": 10, "reference_flags": ["EXECUTED_TRADE_PRICE_REFERENCE"],
        **changes,
    }


@pytest.mark.parametrize("leg", ["eth", "usdc"])
def test_each_conversion_leg_has_an_independent_120_second_age_limit(leg):
    row = converted(**{f"{leg}_timestamp": 880, f"{leg}_available_at": 881})
    boundary = reference_asof([row], 1000, 120, available_by=1001)
    assert boundary["status"] == "PASS"
    assert boundary[f"{leg}_age_seconds"] == 120
    row[f"{leg}_timestamp"] = 879.999
    old = reference_asof([row], 1000, 120, available_by=1001)
    assert old["status"] == "BLOCKED"
    assert old["value"] is None
    assert old["row"] is None
    assert "STALE_REFERENCE_LEG" in old["flags"]


def test_age_is_measured_at_query_instead_of_reusing_age_at_conversion():
    row = converted(usdc_timestamp=890, usdc_available_at=891,
                    eth_age_seconds=0, usdc_age_seconds=110)
    result = reference_asof([row], 1011, 120)
    assert result["status"] == "BLOCKED"
    assert result["eth_age_seconds"] == 11
    assert result["usdc_age_seconds"] == 121


@pytest.mark.parametrize("leg", ["eth", "usdc"])
def test_leg_availability_cannot_be_hidden_by_early_combined_availability(leg):
    row = converted(available_at=1000, **{f"{leg}_available_at": 1010})
    result = reference_asof([row], 1005, 120)
    assert result["status"] == "BLOCKED"
    assert result["value"] is None
    assert "UNAVAILABLE_REFERENCE_LEG" in result["flags"]
    assert reference_asof([row], 1010, 120)["status"] == "PASS"


@pytest.mark.parametrize("leg", ["eth", "usdc"])
def test_leg_observation_cannot_follow_target_even_if_known_by_decision(leg):
    row = converted(**{f"{leg}_timestamp": 1001})
    result = reference_asof([row], 1000, 120, available_by=1010)
    assert result["status"] == "BLOCKED"
    assert "FUTURE_REFERENCE_LEG" in result["flags"]


@pytest.mark.parametrize("indexed", [False, True])
def test_equal_timestamp_aggregate_trades_choose_largest_sequence(indexed):
    rows = [converted(sequence=12, price_usdc_per_eth=2502),
            converted(sequence=10, price_usdc_per_eth=2500),
            converted(sequence=11, price_usdc_per_eth=2501)]
    source = asof_index(rows) if indexed else rows
    result = reference_asof(source, 1001, 120)
    assert result["value"] == 2502
    assert result["row"]["sequence"] == 12


def test_newer_sequence_does_not_override_its_own_late_availability():
    rows = [converted(sequence=10, price_usdc_per_eth=2500),
            converted(sequence=11, price_usdc_per_eth=2501, available_at=1010,
                      eth_available_at=1010)]
    assert reference_asof(asof_index(rows), 1001, 120)["value"] == 2500
    assert reference_asof(asof_index(rows), 1010, 120)["value"] == 2501


@pytest.mark.parametrize("at", [1000, 1001, 1120, 1120.001])
def test_legacy_minute_reference_retains_existing_availability_and_age(at):
    rows = [{"timestamp": 1000, "available_at": 1001, "price_usdc_per_eth": 2500,
             "reference_granularity_seconds": 60}]
    old = backward_asof(rows, at, 120, value_key="price_usdc_per_eth")
    current = reference_asof(rows, at, 120)
    for key in ("row", "value", "age_seconds", "status", "flags"):
        assert current[key] == old[key]
    assert current["eth_age_seconds"] == old["age_seconds"]
    assert current["usdc_age_seconds"] == old["age_seconds"]


def test_markout_target_time_and_information_cutoff_are_distinct():
    rows = [converted(timestamp=400, available_at=401, sequence=10,
                      eth_timestamp=400, usdc_timestamp=400,
                      eth_available_at=401, usdc_available_at=401,
                      price_usdc_per_eth=2500),
            converted(timestamp=401, available_at=402, sequence=11,
                      eth_timestamp=401, usdc_timestamp=401,
                      eth_available_at=402, usdc_available_at=402,
                      price_usdc_per_eth=9999)]
    assert reference_asof(rows, 400, 120)["row"] is None
    mature = reference_asof(rows, 400, 120, available_by=410)
    assert mature["value"] == 2500
    assert mature["row"]["timestamp"] == 400
    assert mature["eth_age_seconds"] == mature["usdc_age_seconds"] == 0


@pytest.mark.parametrize("field", ["eth_timestamp", "usdc_timestamp",
                                   "eth_available_at", "usdc_available_at"])
@pytest.mark.parametrize("value", ["absent", None])
def test_partial_fine_conversion_metadata_blocks_instead_of_falling_back(field, value):
    row = converted()
    if value == "absent":
        del row[field]
    else:
        row[field] = value
    result = reference_asof([row], 1001, 120)
    assert result["status"] == "BLOCKED"
    assert result["row"] is None
    assert result["value"] is None
    assert result["flags"]


def raw_quote(symbol, timestamp, identifier, price, **extra):
    return {"symbol": symbol, "timestamp": timestamp,
            "aggregate_trade_id": identifier, "price": price, **extra}


def test_features_ignore_future_and_late_available_trades_in_both_reference_legs():
    times = list(range(0, 541, 60)) + [590]
    eth = [raw_quote("ETHUSDT", timestamp, index, 2500 + timestamp / 60)
           for index, timestamp in enumerate(times)]
    usdc = [raw_quote("USDCUSDT", timestamp, index, 1)
            for index, timestamp in enumerate(times[:-1])]
    references, _ = convert_aggtrades(eth, usdc)
    states = [{"timestamp": 588, "sqrt_price_x96": sqrt_from_price("2500"),
               "liquidity": 10**18}]
    events = [{"timestamp": 100, "event_type": "Swap", "amount0": 100_000_000,
               "amount1": -10**18, "transaction_hash": "0x100"}]
    config = {"timing": {"lookback_seconds": 540, "block_delay_seconds": 12,
                         "reference_delay_seconds": 1}}
    baseline = build_features([600], events, references, states, config)
    poisoned_eth = deepcopy(eth) + [
        raw_quote("ETHUSDT", 599, 100, "9000", available_at=700),
        raw_quote("ETHUSDT", 601, 101, "1"),
    ]
    poisoned_usdc = deepcopy(usdc) + [
        raw_quote("USDCUSDT", 585, 99, "100", available_at=700),
        raw_quote("USDCUSDT", 599, 100, "0.001", available_at=700),
        raw_quote("USDCUSDT", 601, 101, "50"),
    ]
    poisoned_references, _ = convert_aggtrades(poisoned_eth, poisoned_usdc)
    after = build_features([600], events, poisoned_references, states, config)
    assert after == baseline
    assert baseline[0]["reference_price_usdc_per_eth"] == pytest.approx(2500 + 590 / 60)
    assert baseline[0]["markout_mature_count"] == 1

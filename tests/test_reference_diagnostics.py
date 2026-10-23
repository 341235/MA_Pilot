"""Small fixtures for the cache-only reference resolution checks."""
from decimal import Decimal

import pytest

from lp_pilot.data import DataContractError
from lp_pilot.fine_reference import convert_aggtrades
from lp_pilot.reference_diagnostics import (_trace, aggregate_minutes, common_markouts,
                                            compare_minutes, compare_variants, make_minute_references)
from lp_pilot.storage import read_table, write_table
from lp_pilot.temporal import asof_index, reference_asof, utc_timestamp


START_MS = 1_705_276_800_000  # 2024-01-15 00:00 UTC
START = START_MS / 1000


def trade(offset_ms, identity, price):
    raw_time = START_MS + offset_ms
    return {"source_timestamp_raw": raw_time, "timestamp": raw_time / 1000,
            "aggregate_trade_id": identity, "price": str(price)}


def kline(open_time, price, **changes):
    return {"open_time": open_time, "timestamp": open_time + 60,
            "close_time": open_time + 59.999, "close": str(price), **changes}


def test_half_open_minutes_keep_actual_last_trade_separate_from_completion():
    rows = [trade(0, 1, "100"), trade(59_999, 2, "101"),
            trade(59_999, 3, "102"), trade(60_000, 4, "103")]
    result = aggregate_minutes(rows, "ETHUSDT")
    assert [row["close"] for row in result] == ["102", "103"]
    first, second = result
    assert first["open_time"] == START
    assert first["timestamp"] == first["candle_completed_at"] == START + 60
    assert first["last_trade_timestamp"] == START + 59.999
    assert first["last_aggregate_trade_id"] == 3
    assert first["aggregate_trade_count"] == 3
    assert second["open_time"] == second["last_trade_timestamp"] == START + 60
    assert second["timestamp"] == START + 120


@pytest.mark.parametrize("rows", [
    [trade(2, 1, "100"), trade(1, 2, "101")],
    [trade(1, 2, "100"), trade(1, 1, "101")],
])
def test_minute_aggregation_rejects_decreasing_time_or_same_time_sequence(rows):
    with pytest.raises(DataContractError):
        aggregate_minutes(rows, "ETHUSDT")


def test_absent_trade_minutes_are_not_filled_with_a_previous_price():
    own = aggregate_minutes([trade(1, 1, "100"), trade(120_001, 2, "102")], "ETHUSDT")
    assert [row["open_time"] for row in own] == [START, START + 120]
    compared = compare_minutes(own, [kline(START, "100"), kline(START + 60, "100")])
    assert compared["missing_trade_minutes"] == [START + 60]
    assert compared["missing_kline_minutes"] == [START + 120]
    assert compared["exact_price_matches"] == 1


def test_price_comparison_reports_small_decimal_difference_without_tolerance():
    own = aggregate_minutes([trade(1, 1, "100.000000000001")], "ETHUSDT")
    compared = compare_minutes(own, [kline(START, "100")])
    assert compared["price_mismatch_count"] == 1
    assert compared["time_mapping_error_count"] == 0
    row = compared["rows"][0]
    assert row["prices_equal"] is False
    assert abs(Decimal(row["raw_close_difference"])) == Decimal("0.000000000001")


def test_time_mapping_errors_are_reported_even_when_numeric_prices_match():
    own = aggregate_minutes([trade(59_000, 1, "100")], "ETHUSDT")
    compared = compare_minutes(own, [kline(START, "100", timestamp=START + 59.999)])
    assert compared["exact_price_matches"] == 1
    assert compared["price_mismatch_count"] == 0
    assert compared["time_mapping_error_count"] == 1


def test_each_currency_leg_is_closed_before_conversion():
    eth = [trade(10_000, 1, "100")]
    usdc = [trade(5_000, 1, "1"), trade(50_000, 2, "2")]
    legs = {"ETHUSDT": aggregate_minutes(eth, "ETHUSDT"),
            "USDCUSDT": aggregate_minutes(usdc, "USDCUSDT")}
    own = make_minute_references(legs, delay_seconds=1)
    fine, _ = convert_aggtrades(eth, usdc, delay_seconds=1)
    # A later USDC trade changes the minute close, but was unavailable at the ETH trade.
    assert Decimal(str(own[0]["price_usdc_per_eth"])) == 50
    assert fine[-1]["price_usdc_per_eth"] == 100
    assert own[0]["eth_last_trade_timestamp"] == START + 10
    assert own[0]["usdc_last_trade_timestamp"] == START + 50
    assert own[0]["eth_timestamp"] == own[0]["usdc_timestamp"] == START + 60
    assert own[0]["eth_available_at"] == own[0]["usdc_available_at"] == START + 61


def test_closed_candle_availability_and_freshness_use_completion_time():
    legs = {symbol: aggregate_minutes([trade(1, 1, price)], symbol)
            for symbol, price in (("ETHUSDT", "100"), ("USDCUSDT", "1"))}
    refs = asof_index(make_minute_references(legs, delay_seconds=1))
    assert reference_asof(refs, START + 60, 120)["row"] is None
    assert reference_asof(refs, START + 60, 120, available_by=START + 61)["value"] == 100
    assert reference_asof(refs, START + 59.999, 120, available_by=START + 61)["row"] is None
    ready = reference_asof(refs, START + 61, 120)
    assert ready["value"] == 100
    assert ready["eth_age_seconds"] == ready["usdc_age_seconds"] == 1
    assert reference_asof(refs, START + 180, 120)["row"] is not None
    assert reference_asof(refs, START + 180.001, 120)["row"] is None


def test_saved_minute_trace_retains_actual_trade_time_after_parquet_roundtrip(tmp_path):
    legs = {symbol: aggregate_minutes([trade(1, 1, price)], symbol)
            for symbol, price in (("ETHUSDT", "100"), ("USDCUSDT", "1"))}
    references = make_minute_references(legs, delay_seconds=1)
    path = tmp_path / "reference_prices.parquet"
    write_table(path, references)
    saved = read_table(path)
    assert saved[0]["reference_granularity_seconds"] == "60"
    lookup = {symbol: {row["timestamp"]: row for row in minutes} for symbol, minutes in legs.items()}
    args = (START + 61, START + 61, {"max_price_age_seconds": 120}, lookup)
    trace = _trace(asof_index(saved), *args)
    assert trace == _trace(asof_index(references), *args)
    for leg in trace["legs"].values():
        assert utc_timestamp(leg["observation_time"]) == START + 60
        assert utc_timestamp(leg["actual_last_trade_time"]) == START + .001
        assert utc_timestamp(leg["available_at"]) == START + 61
        assert leg["observation_kind"] == "candle completion"


def test_three_way_markouts_use_intersection_and_identical_raw_quote_weights():
    settings = {"timing": {"block_delay_seconds": 12, "reference_delay_seconds": 1,
                "max_price_age_seconds": 5, "lookback_seconds": 3600, "markout_seconds": 300}}
    events = [{"timestamp": at, "event_type": "Swap", "transaction_hash": f"0x{at}",
               "log_index": 0, "amount0": volume * 10**6,
               "amount1": -10**18 if at < 130 else 10**18}
              for at, volume in ((100, 100), (110, 200), (120, 500), (130, 300))]
    starts = [{"timestamp": at, "price_usdc_per_eth": 100} for at in (100, 110, 120, 130)]
    endpoints = {
        "A": [(400, 101), (410, 102), (420, 102), (430, 103)],
        "B": [(400, 102), (420, 103), (430, 104)],
        "C": [(400, 100.5), (410, 103), (430, 99)],
    }
    references = {label: asof_index(starts + [
        {"timestamp": at, "price_usdc_per_eth": price} for at, price in values])
        for label, values in endpoints.items()}
    result = common_markouts([450], events, references, settings)
    row = result["decision_rows"][0]
    assert row["valid_counts"] == {"A": 4, "B": 3, "C": 3}
    assert row["common_valid_swaps"] == 2
    assert row["common_identities"] == ["0x100:0", "0x130:0"]
    assert row["excluded_identities"] == {"A": [], "B": ["0x110:0"], "C": ["0x120:0"]}
    assert row["lost_to_intersection"] == {
        "A": ["0x110:0", "0x120:0"], "B": ["0x120:0"], "C": ["0x110:0"]}
    assert Decimal(row["common_quote_volume_usdc"]) == 400
    assert row["A_markout_bps"] == pytest.approx(-200)
    assert row["B_markout_bps"] == pytest.approx(-250)
    assert row["C_markout_bps"] == pytest.approx(87.5)
    assert result["totals"]["common_valid_swaps"] == 2


def test_identical_reference_variants_report_identical_swap_membership():
    settings = {"timing": {"block_delay_seconds": 12, "reference_delay_seconds": 1,
                "max_price_age_seconds": 5, "lookback_seconds": 3600, "markout_seconds": 300}}
    events = [{"timestamp": 100, "event_type": "Swap", "transaction_hash": "0x100",
               "log_index": 0, "amount0": 100_000_000, "amount1": -10**18}]
    refs = asof_index([{"timestamp": 100, "price_usdc_per_eth": 100},
                       {"timestamp": 400, "price_usdc_per_eth": 101}])
    result = common_markouts([420], events, dict.fromkeys(("A", "B", "C"), refs), settings)
    assert result["membership_identical"] is True
    row = result["decision_rows"][0]
    assert row["valid_counts"] == {"A": 1, "B": 1, "C": 1}
    assert row["common_valid_swaps"] == 1
    assert row["lost_to_intersection"] == {"A": [], "B": [], "C": []}
    assert [row[f"{label}_markout_bps"] for label in ("A", "B", "C")] == pytest.approx([100] * 3)


def test_individually_valid_variants_with_empty_intersection_keep_null_markouts():
    settings = {"timing": {"block_delay_seconds": 12, "reference_delay_seconds": 1,
                "max_price_age_seconds": 5, "lookback_seconds": 3600, "markout_seconds": 300}}
    events = [{"timestamp": at, "event_type": "Swap", "transaction_hash": f"0x{at}",
               "log_index": 0, "amount0": 100_000_000, "amount1": -10**18}
              for at in (100, 110, 120)]
    starts = [{"timestamp": at, "price_usdc_per_eth": 100} for at in (100, 110, 120)]
    refs = {label: asof_index(starts + [{"timestamp": endpoint, "price_usdc_per_eth": 101}])
            for label, endpoint in (("A", 400), ("B", 410), ("C", 420))}
    result = common_markouts([450], events, refs, settings)
    row = result["decision_rows"][0]
    assert row["valid_counts"] == {"A": 1, "B": 1, "C": 1}
    assert row["common_valid_swaps"] == result["totals"]["common_valid_swaps"] == 0
    assert row["common_identities"] == []
    assert Decimal(row["common_quote_volume_usdc"]) == 0
    assert all(row[f"{label}_markout_bps"] is None for label in ("A", "B", "C"))
    assert result["membership_identical"] is False


@pytest.mark.parametrize("missing_deviation_variant", [None, "C"])
def test_comparison_masks_require_all_variants_and_use_common_sample_markouts(missing_deviation_variant):
    deviation = "dex_cex_deviation_bps"
    absolute = "absolute_dex_cex_deviation_bps"
    markout = "historical_markout_5m_bps"
    measured = {label: [{"decision_time": 450, deviation: 5, absolute: 5, markout: 100}]
                for label in ("A", "B", "C")}
    if missing_deviation_variant:
        measured[missing_deviation_variant][0][deviation] = None
        measured[missing_deviation_variant][0][absolute] = None
    common = {"decision_rows": [{"decision_time": 450, "A_markout_bps": None,
                                "B_markout_bps": None, "C_markout_bps": None}]}
    comparisons, masks = compare_variants(measured, common, [450])
    assert masks[markout] == {"included": [], "excluded": [
        {"decision_time": 450, "missing_variants": ["A", "B", "C"]}]}
    for metric in (deviation, absolute):
        assert masks[metric] == ({"included": [], "excluded": [
            {"decision_time": 450, "missing_variants": ["C"]}]}
            if missing_deviation_variant else {"included": [450], "excluded": []})
        assert all(pair[metric]["comparable_count"] == (0 if missing_deviation_variant else 1)
                   for pair in comparisons.values())
    assert all(pair[markout]["comparable_count"] == 0 for pair in comparisons.values())

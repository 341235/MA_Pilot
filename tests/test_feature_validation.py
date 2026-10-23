"""Comparison statistics and the fixed-sample, cache-only measurement study."""
from datetime import datetime, timezone
import json

import pytest

from lp_pilot.feature_validation import (common_trade_markouts, compare_measurements,
                                         proxy_measurements, validate_features)
from lp_pilot.features import build_features
from lp_pilot.storage import sha256, write_json, write_table
from lp_pilot.temporal import apply_availability, asof_index
from lp_pilot.uniswap import sqrt_from_price


METRIC = "historical_markout_5m_bps"


def cfg(root=None):
    return {"_root": str(root) if root else ".", "pool": {},
        "pilot": {"start": "2024-01-15T00:00:00Z", "end": "2024-01-17T00:00:00Z"}, "timing": {
        "block_delay_seconds": 12, "reference_delay_seconds": 1,
        "max_price_age_seconds": 120, "max_pool_age_seconds": 120,
        "lookback_seconds": 3600, "markout_seconds": 300},
        "data": {"output_dir": "data/runs"},
        "feature_validation": {"day": "2024-01-15", "alternate_block_delay_seconds": 60}}


def event(at=100, **fields):
    return {"timestamp": at, "event_type": "Swap", "amount0": 100_000_000,
            "amount1": -10**18, "transaction_hash": f"0x{at}", "log_index": 0, **fields}


def state(at=488):
    return {"timestamp": at, "sqrt_price_x96": sqrt_from_price("100"),
            "liquidity": 10**18, "state_changed_at": 300, "block_number": 100}


def test_common_decisions_exclude_nulls_and_keep_zero_as_a_distinct_sign():
    left = [{"decision_time": t, METRIC: value} for t, value in enumerate((-1, 0, 2, None, 4))]
    right = [{"decision_time": t, METRIC: value} for t, value in enumerate((-3, 1, 4, 8, None))]
    result = compare_measurements(left, right, METRIC)
    assert result["candidate_decisions"] == 5
    assert result["comparable_count"] == 3
    assert result["excluded_count"] == 2
    assert result["median_absolute_difference_bps"] == 2
    assert result["p95_absolute_difference_bps"] == 2
    assert result["sign_agreement_count"] == 2
    assert result["exclusion_reasons"]["left_missing"] == 1
    assert result["exclusion_reasons"]["right_missing"] == 1


def test_reported_p95_uses_the_stated_linear_interpolation_convention():
    left = [{"decision_time": 0, METRIC: 0}, {"decision_time": 1, METRIC: 0}]
    right = [{"decision_time": 0, METRIC: 0}, {"decision_time": 1, METRIC: 10}]
    result = compare_measurements(left, right, METRIC)
    assert result["median_absolute_difference_bps"] == 5
    assert result["p95_absolute_difference_bps"] == pytest.approx(9.5)


def test_no_common_valid_values_do_not_generate_zero_differences_or_agreement():
    result = compare_measurements([{"decision_time": 1, METRIC: None}],
                                  [{"decision_time": 1, METRIC: 100}], METRIC)
    assert result["comparable_count"] == 0
    assert result["median_absolute_difference_bps"] is None
    assert result["p95_absolute_difference_bps"] is None
    assert result["sign_agreement_fraction"] is None


def test_efficient_proxy_path_matches_production_and_preserves_horizon_anchor():
    references = [{"timestamp": 100, "price_usdc_per_eth": 100},
                  {"timestamp": 400, "price_usdc_per_eth": 101},
                  {"timestamp": 450, "price_usdc_per_eth": 200}]
    settings = cfg()
    index = asof_index(apply_availability(references, 1))
    measured = proxy_measurements([500], [event()], index, [state()], settings, block_delay_seconds=12)[0]
    production = build_features([500], [event()], references, [state()], settings)[0]
    for metric in (METRIC, "dex_cex_deviation_bps", "absolute_dex_cex_deviation_bps"):
        assert measured[metric] == production[metric]
    assert measured[METRIC] == pytest.approx(100)
    assert measured["reference_timestamp"] == 450
    assert measured["eth_age_seconds"] == measured["usdc_age_seconds"] == 50


def test_common_trade_reweighting_separates_price_differences_from_membership_changes():
    settings = cfg()
    settings["timing"]["max_price_age_seconds"] = 5
    events = [event(100), event(110, amount0=-300_000_000, amount1=10**18)]
    initial = [{"timestamp": 100, "price_usdc_per_eth": 100},
               {"timestamp": 110, "price_usdc_per_eth": 100}]
    left = initial + [{"timestamp": 400, "price_usdc_per_eth": 101},
                      {"timestamp": 410, "price_usdc_per_eth": 101}]
    right = initial + [{"timestamp": 400, "price_usdc_per_eth": 101}]
    result = common_trade_markouts([420], events, asof_index(left), asof_index(right),
                                   settings, block_delay_seconds=12)
    assert result["membership_identical"] is False
    assert result["totals"]["left_valid_swaps"] == 2
    assert result["totals"]["right_valid_swaps"] == 1
    row = result["decision_rows"][0]
    assert row["common_quote_volume_usdc"] == 100
    assert row["left_common_markout_bps"] == pytest.approx(100)
    assert row["right_common_markout_bps"] == pytest.approx(100)
    assert row["left_only_identities"] == ["0x110:0"]
    assert result["comparison"]["median_absolute_difference_bps"] == 0


def test_identical_common_trade_membership_is_verified_instead_of_assumed():
    references = asof_index([{"timestamp": 100, "price_usdc_per_eth": 100},
                             {"timestamp": 400, "price_usdc_per_eth": 101}])
    result = common_trade_markouts([420], [event()], references, references, cfg(), block_delay_seconds=12)
    assert result["membership_identical"] is True
    assert result["totals"]["common_valid_swaps"] == 1
    assert result["comparison"]["sign_agreement_fraction"] == 1


def test_missing_fine_cache_keeps_minute_results_and_performs_no_download(tmp_path, monkeypatch):
    settings = cfg(tmp_path)
    start = datetime(2024, 1, 15, tzinfo=timezone.utc).timestamp()
    decisions = [start + hour * 3600 for hour in range(24)]
    references = [{"timestamp": stamp, "price_usdc_per_eth": 100}
                  for stamp in range(int(start) - 3660, int(decisions[-1]) + 60, 60)]
    observations = [state(decision - 12) for decision in decisions]
    events = [event(start - 600)]
    features = build_features(decisions, events, references, observations, settings)
    directory = tmp_path / "data/runs/real"
    tables = {"events": events, "pool_observations": observations,
              "reference_prices": references, "features": features,
              "outcomes": [{"decision_time": decisions[0], "y_usdc": None, "sensitivity_y_usdc": 1}]}
    for name, rows in tables.items():
        write_table(directory / f"{name}.parquet", rows)
    write_json(directory / "manifest.json", {"mode": "real", "scope": "full", "status": "WARN",
               "diagnostics": {}, "query_parameters": {key: settings[key] for key in ("pilot", "pool", "timing")},
               "artifacts": {path.name: sha256(path) for path in directory.glob("*.parquet")}})
    monkeypatch.setattr("lp_pilot.fine_reference.requests.Session", lambda: pytest.fail("Validation attempted network access"))
    report = validate_features(settings)
    assert report["status"] == "WARN"
    assert report["fine_status"] == "BLOCKED"
    assert report["network_requests"] == 0
    assert "Missing cached fine reference evidence" in report["fine_error"]
    assert report["minute_formula_regression"]["status"] == "PASS"
    assert report["minute_formula_regression"]["comparisons"] == 72
    assert report["selection"]["decision_times"] == decisions
    assert report["granularity_comparisons"] == {}
    assert set(report["variant_counts"]) == {"minute_baseline", "minute_delay_60s"}
    assert (tmp_path / "reports/feature_validation.md").is_file()
    assert report["real_run"]["primary_y_nonmissing"] == 0
    assert list((tmp_path / "reports").iterdir()) == [tmp_path / "reports/feature_validation.md"]
    assert not (tmp_path / "data/validation/features").exists()


def test_changed_existing_run_artifact_is_rejected_before_any_reference_work(tmp_path):
    directory = tmp_path / "data/runs/real"
    write_table(directory / "events.parquet", [event()])
    settings = cfg(tmp_path)
    write_json(directory / "manifest.json", {"mode": "real", "status": "WARN", "artifacts": {},
               "query_parameters": {key: settings[key] for key in ("pilot", "pool", "timing")}})
    with pytest.raises(ValueError, match="differs from its recorded run artifact"):
        validate_features(settings)


def test_core_run_parameters_cannot_change_silently_for_the_comparison(tmp_path):
    settings = cfg(tmp_path)
    directory = tmp_path / "data/runs/real"
    write_json(directory / "manifest.json", {"mode": "real", "status": "WARN", "artifacts": {},
               "query_parameters": {key: settings[key] for key in ("pilot", "pool", "timing")}})
    settings["timing"]["reference_delay_seconds"] = 10
    with pytest.raises(ValueError, match="differs from the recorded real run parameters"):
        validate_features(settings)

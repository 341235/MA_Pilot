from pathlib import Path

import pytest

from lp_pilot.config import decisions, load_config, required_interval, utc_seconds
from lp_pilot.storage import read_table, write_table
from lp_pilot.validation import Check, final_status


def test_default_interval_and_exclusive_end():
    cfg = load_config(Path(__file__).resolve().parents[1] / "configs/pilot.toml")
    grid = decisions(cfg)
    start, end = required_interval(cfg)
    assert len(grid) == 48
    assert start == grid[0] - 86400
    assert grid[-1] < utc_seconds(cfg["pilot"]["end"])
    assert end > grid[-1] + 3600 + cfg["timing"]["execution_delay_seconds"]


def test_naive_timestamp_rejected():
    with pytest.raises(ValueError, match="UTC"):
        utc_seconds("2024-01-15T00:00:00")


def test_lossless_parquet_uint256_uint160(tmp_path):
    rows = [{"timestamp": 1705276800, "liquidity": 2**128 - 1,
             "sqrt_price_x96": 2**160 - 1, "fee_growth_global0_x128": 2**256 - 1}]
    path = tmp_path / "events.parquet"
    write_table(path, rows)
    restored = read_table(path)[0]
    assert restored["timestamp"] == rows[0]["timestamp"]
    for key in ("liquidity", "sqrt_price_x96", "fee_growth_global0_x128"):
        assert isinstance(restored[key], str)
        assert int(restored[key]) == rows[0][key]


def test_missing_critical_check_is_blocked():
    assert final_status([Check("fees", "PASS", "fixture")], ["fees", "costs"], "real") == "BLOCKED"


def test_parquet_preserves_columns_missing_from_first_row(tmp_path):
    path = tmp_path / "mixed.parquet"
    write_table(path, [{"status": "BLOCKED"}, {"status": "WARN", "position_liquidity": 2**100}])
    rows = read_table(path)
    assert rows[0]["position_liquidity"] is None
    assert rows[1]["position_liquidity"] == str(2**100)


def test_synthetic_and_warnings_cannot_pass_empirical_gate():
    assert final_status([Check("fees", "PASS", "fixture")], ["fees"], "synthetic") == "WARN"
    assert final_status([Check("fees", "WARN", "approximation")], ["fees"], "real") == "WARN"


def test_failed_noncritical_check_still_fails_run():
    assert final_status([Check("fees", "PASS", "ok"), Check("invariant", "FAIL", "bad")], ["fees"], "real") == "FAIL"

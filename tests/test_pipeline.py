"""Integration tests using artificial pool data."""
from copy import deepcopy
import json
from pathlib import Path

from lp_pilot.config import load_config
from lp_pilot.pipeline import run
from lp_pilot.storage import read_table, sha256


def small_config(tmp_path):
    cfg = deepcopy(load_config(Path(__file__).resolve().parents[1] / "configs/pilot.toml"))
    cfg["_root"] = str(tmp_path)
    cfg["pilot"]["end"] = "2024-01-15T01:00:00Z"
    return cfg


def test_real_mode_missing_inputs_never_uses_synthetic(tmp_path):
    cfg = small_config(tmp_path)
    result = run(cfg, "real")
    assert result["status"] == "BLOCKED"
    assert result["empirical_end_to_end"] is False
    assert result["rows"]["events"] == 0
    outcomes = read_table(Path(result["output"]) / "outcomes.parquet")
    assert len(outcomes) == 3
    assert all(row["y_usdc"] is None and row["fees0_usdc"] is None for row in outcomes)
    assert all(row["status"] == "BLOCKED" for row in outcomes)
    report = Path(result["report"]).read_text()
    assert "NOT ESTABLISHED" in report
    features = read_table(Path(result["output"]) / "features.parquet")
    assert all(row["swap_count"] is None and row["swap_volume_usdc"] is None for row in features)
    assert "Reference source: unavailable" in report


def test_synthetic_end_to_end_and_deterministic_tables(tmp_path):
    cfg = small_config(tmp_path)
    first = run(cfg, "synthetic")
    assert first["status"] == "WARN"
    assert first["rows"]["events"] > 1440
    assert first["rows"]["states"] > 1440
    assert first["rows"]["outcomes"] == 3
    output = Path(first["output"])
    rows = read_table(output / "outcomes.parquet")
    assert all(row["sensitivity_y_usdc"] is not None for row in rows)
    assert all(row["y_usdc"] is None for row in rows)
    assert all(row["end_value_ex_fees_usdc"] is not None for row in rows)
    before = {p.name: sha256(p) for p in output.glob("*.parquet")}
    second = run(cfg, "synthetic")
    after = {p.name: sha256(p) for p in output.glob("*.parquet")}
    assert before == after
    assert second["empirical_end_to_end"] is False
    manifest = json.loads((output / "manifest.json").read_text())
    assert {p.name for p in output.iterdir()} == {"manifest.json", *(f"{name}.parquet" for name in second["rows"])}
    assert len(manifest["checks"]) > 0
    assert Path(second["report"]) == tmp_path / "reports/pilot_report.md"
    assert list((tmp_path / "reports").rglob("*.md")) == [Path(second["report"])]
    assert manifest["sources"][0]["empirical"] is False
    assert manifest["diagnostics"]["primary_y_nonmissing"] == 0
    report = Path(second["report"]).read_text()
    assert "No Binance observations are used in this synthetic run" in report


def test_configured_empirical_cost_constants_cannot_fabricate_calibration(tmp_path):
    cfg = small_config(tmp_path)
    cfg["costs"]["mode"] = "empirical"
    # All illustrative constants remain populated, but receipts lack attribution.
    result = run(cfg, "synthetic")
    assert result["status"] == "BLOCKED"
    output = Path(result["output"])
    outcomes = read_table(output / "outcomes.parquet")
    assert all(row["y_usdc"] is None for row in outcomes)
    assert all(row["net_lp_pnl_usdc"] is None for row in outcomes)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["cost_model"]["token_conversion_bps"] is None
    assert manifest["cost_model"]["sample_counts"] == {"burn": 0, "collect": 0, "mint": 0}

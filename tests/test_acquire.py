"""RPC acquisition budgets and the past-only receipt sample."""
import json
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from lp_pilot.acquire import _receipt_sample, make_rpc_client
from lp_pilot import acquire, replay, rpc_events
from lp_pilot.config import data_path, load_config, required_interval, utc_seconds
from lp_pilot.rpc import RpcError
from lp_pilot.storage import read_table, sha256


def test_missing_credentials_do_not_fall_back_to_other_provider(monkeypatch):
    cfg = load_config(Path(__file__).resolve().parents[1] / "configs/pilot.toml")
    monkeypatch.delenv("ETH_RPC_URL", raising=False)
    monkeypatch.delenv("ARCHIVE_RPC_URL", raising=False)
    with pytest.raises(RpcError, match="Set ETH_RPC_URL"):
        make_rpc_client(cfg)


def test_client_enforces_configured_acquisition_ceilings(monkeypatch, tmp_path):
    cfg = load_config(Path(__file__).resolve().parents[1] / "configs/pilot.toml")
    cfg["_root"] = str(tmp_path)
    monkeypatch.setenv("ETH_RPC_URL", "https://private.invalid/private-secret")
    client = make_rpc_client(cfg)
    assert client.max_compute_units == cfg["acquisition"]["max_compute_units"]
    assert client.budget.max_requests == cfg["acquisition"]["max_requests"]
    assert client.budget.max_bytes == cfg["acquisition"]["max_download_bytes"]
    assert "private-secret" not in str(client.statistics())


def test_receipt_sample_prioritizes_available_past_swap_and_keeps_its_budget():
    cfg = load_config(Path(__file__).resolve().parents[1] / "configs/pilot.toml")
    cfg["pilot"]["end"] = "2024-01-15T01:00:00Z"
    at = utc_seconds(cfg["pilot"]["start"])
    cfg["acquisition"]["max_receipts"] = 2
    def row(timestamp, kind, block):
        return {"timestamp": timestamp, "event_type": kind, "block_number": block}
    by_hash = {"known": [row(at-13, "Swap", 10)], "not_yet_available": [row(at-1, "Swap", 11)],
               "future": [row(at+1, "Swap", 12)], "mint": [row(at-1000, "Mint", 1)]}
    assert _receipt_sample(cfg, by_hash) == ["known", "mint"]


def test_full_acquisition_saves_configured_inputs_without_running_analysis(monkeypatch, tmp_path):
    cfg = load_config(Path(__file__).resolve().parents[1] / "configs/pilot.toml")
    cfg["_root"] = str(tmp_path)
    client = Mock()
    client.statistics.return_value = {"logical_requests": 0}
    events = [{"timestamp": required_interval(cfg)[0] + 12, "block_number": 42, "event_type": "Swap"}]
    download = Mock(return_value=(events, {"block_start": 40, "block_end_exclusive": 44}))
    inputs = Mock()
    inputs.snapshot.return_value = {"ticks": {}, "bitmap_words": {}}
    inputs.checkpoints.return_value = 2
    inputs.receipts.return_value = {"sampled_transactions": 1}
    monkeypatch.setattr(acquire, "make_rpc_client", lambda cfg: client)
    monkeypatch.setattr(acquire, "verify_pool_identity", lambda client, address: {"status": "PASS"})
    monkeypatch.setattr(rpc_events, "download_events", download)
    monkeypatch.setattr(acquire, "_save_snapshot", inputs.snapshot)
    monkeypatch.setattr(acquire, "_save_checkpoints", inputs.checkpoints)
    monkeypatch.setattr(acquire, "_save_receipts", inputs.receipts)
    monkeypatch.setattr(replay, "replay_events", lambda *args: pytest.fail("Acquisition ran the analysis"))

    result = acquire.fetch_rpc(cfg)

    download.assert_called_once_with(client, *required_interval(cfg), pool_address=cfg["pool"]["address"],
                                     chunk_blocks=cfg["acquisition"]["chunk_blocks"], progress=None)
    assert inputs.mock_calls == [call.snapshot(cfg, client, 41), call.checkpoints(cfg, client, events, 44),
                                 call.receipts(cfg, client, events)]
    path = data_path(cfg, "events_path")
    manifest = json.loads(data_path(cfg, "export_manifest_path").read_text())
    assert read_table(path) == events
    assert (manifest["source"], manifest["sha256"], manifest["pool_address"], manifest["chain_id"]) == (
        "ethereum_rpc", sha256(path), cfg["pool"]["address"].lower(), 1)
    assert (result["status"], result["event_count"], result["checkpoint_count"]) == ("PASS", 1, 2)
    assert not {"replay_checks", "checkpoint_check"} & result.keys()

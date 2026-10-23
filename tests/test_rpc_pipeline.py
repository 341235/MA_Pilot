"""Artificial RPC archives for provenance and checkpoint tests."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from lp_pilot.config import load_config
from lp_pilot.data import DataContractError, EVENT_TOPICS, POOL, normalize_event
from lp_pilot.pipeline import _checkpoint_checks, run
from lp_pilot.rpc import SELECTORS
from lp_pilot.rpc_ingest import load_rpc_export, validate_rpc_snapshot
from lp_pilot.storage import sha256, write_json, write_table


def encode(*values):
    return "0x" + "".join(format(value % (1 << 256), "064x") for value in values)


def header(number):
    return {"block_number": number, "block_hash": "0x" + format(number, "064x"),
            "timestamp": 1000 + (number - 100) * 12}


def event_export(tmp_path, *, empty=False):
    raw = {"address": POOL, "blockNumber": hex(101), "blockHash": header(101)["block_hash"],
           "transactionHash": "0x" + "aa" * 32, "transactionIndex": "0x0", "logIndex": "0x0",
           "topics": [EVENT_TOPICS["Collect"], encode(1), encode(-10), encode(10)],
           "data": encode(2, 0, 0), "removed": False}
    logs = [] if empty else [raw]
    events = [normalize_event({**row, "timestamp": 1012}) for row in logs]
    path, sidecar = tmp_path / "events.parquet", tmp_path / "export_manifest.json"
    write_table(path, events)
    chunks = []
    for first, last, result in ((100, 101, logs), (102, 102, [])):
        artifact = tmp_path / f"chunk_{first}.json"
        write_json(artifact, {"method": "eth_getLogs", "params": [{"address": POOL, "fromBlock": hex(first), "toBlock": hex(last)}], "result": result})
        chunks.append({"from_block": first, "to_block": last, "row_count": len(result),
                       "raw_logs_sha256": hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                       "evidence_path": artifact.name, "evidence_sha256": sha256(artifact)})
    manifest = {"source": "ethereum_rpc", "chain_id": 1, "pool_address": POOL,
                "coverage_start": 1000, "coverage_end": 1036, "block_start": 100, "block_end_exclusive": 103,
                "boundary_headers": [header(n) for n in (99, 100, 102, 103)],
                "block_headers": [header(n) for n in range(99, 104)], "chunks": chunks,
                "source_row_count": len(events), "normalized_row_count": len(events), "coverage_complete": True,
                "retrieved_at": "2026-10-22T00:00:00Z", "sha256": sha256(path)}
    write_json(sidecar, manifest)
    return path, sidecar, manifest


def snapshot_export(tmp_path):
    number, block_hash = 100, header(100)["block_hash"]
    identifier = {"blockHash": block_hash, "requireCanonical": True}

    def call(name, values, argument=None):
        selector = SELECTORS[name]
        if argument is not None:
            selector += format(argument % (1 << 256), "064x")
        return {"method": "eth_call", "params": [{"to": POOL, "data": selector}, identifier], "result": encode(*values)}

    raw_header = {"number": hex(number), "hash": block_hash, "timestamp": hex(1000)}
    evidence = {"source_kind": "historical_rpc", "chain_id": 1, "pool_address": POOL,
                "block_number": number, "block_hash": block_hash, "block_identifier": identifier,
                "block_header_before": raw_header, "block_header_after": raw_header,
                "calls": {"slot0": call("slot0", (2**96, 0, 0, 1, 1, 0, 1)),
                          "liquidity": call("liquidity", (100,)), "tickSpacing": call("tickSpacing", (10,)),
                          "fee": call("fee", (500,)), "feeGrowthGlobal0X128": call("feeGrowthGlobal0X128", (100,)),
                          "feeGrowthGlobal1X128": call("feeGrowthGlobal1X128", (200,)), "tickBitmap": {}, "ticks": {}}}
    snapshot = {"chain_id": 1, "pool_address": POOL, "block_number": number, "block_hash": block_hash,
                "timestamp": 1000, "sqrt_price_x96": str(2**96), "tick": 0, "liquidity": "100",
                "fee_growth_global0_x128": "100", "fee_growth_global1_x128": "200",
                "fee_protocol0": 0, "fee_protocol1": 0, "tick_spacing": 10, "fee_pips": 500,
                "ticks_complete": True, "provenance": "artificial saved RPC fixture", "bitmap_words": {}, "ticks": {}}
    for word in range(-347, 347):
        value = 1 << 255 if word == -1 else 2 if word == 0 else 0
        snapshot["bitmap_words"][str(word)] = str(value)
        evidence["calls"]["tickBitmap"][str(word)] = call("tickBitmap", (value,), word)
    for tick, net in ((-10, 100), (10, -100)):
        snapshot["ticks"][str(tick)] = {"liquidity_gross": "100", "liquidity_net": str(net),
            "fee_growth_outside0_x128": "3", "fee_growth_outside1_x128": "4", "initialized": True}
        evidence["calls"]["ticks"][str(tick)] = call("ticks", (100, net, 3, 4, 0, 0, 0, 1), tick)
    path, evidence_path = tmp_path / "snapshot.json", tmp_path / "snapshot_evidence.json"
    write_json(path, snapshot)
    write_json(evidence_path, evidence)
    manifest = {"source": "ethereum_rpc_snapshot", "complete": True, "snapshot_sha256": sha256(path),
                "evidence_path": evidence_path.name, "evidence_sha256": sha256(evidence_path),
                "bitmap_word_min": -347, "bitmap_word_max": 346, "bitmap_words_count": 694, "initialized_tick_count": 2}
    write_json(path.with_name(path.name + ".manifest.json"), manifest)
    return path, snapshot, evidence_path, evidence, manifest


def config(tmp_path):
    cfg = deepcopy(load_config(Path(__file__).resolve().parents[1] / "configs/pilot.toml"))
    cfg["_root"] = str(tmp_path)
    cfg["pilot"]["end"] = "2024-01-15T01:00:00Z"
    cfg["data"].update(source="rpc", events_path="events.parquet", export_manifest_path="export_manifest.json",
                        snapshot_path="snapshot.json", checkpoints_path="checkpoints.json", receipts_path="receipts.json")
    return cfg


def test_rpc_export_reconciles_raw_artifacts_and_zero_event_chunks(tmp_path):
    path, sidecar, manifest = event_export(tmp_path)
    rows, checked = load_rpc_export(path, sidecar)
    assert len(rows) == checked["validated_row_count"] == 1
    assert rows[0]["event_type"] == "Collect"
    assert checked["source"] == "ethereum_rpc"
    assert "query_id" not in checked
    assert checked["chunks"][1]["row_count"] == 0


@pytest.mark.parametrize("change,match", [
    ({"source": "dune"}, "source"), ({"chain_id": 10}, "chain"),
    ({"coverage_complete": False}, "incomplete"), ({"source_row_count": 2}, "row count"),
    ({"sha256": "0" * 64}, "checksum"), ({"block_end_exclusive": 104}, "boundary"),
])
def test_rpc_export_rejects_invalid_manifest(tmp_path, change, match):
    path, sidecar, manifest = event_export(tmp_path)
    write_json(sidecar, {**manifest, **change})
    with pytest.raises(DataContractError, match=match):
        load_rpc_export(path, sidecar)


@pytest.mark.parametrize("case", ["gap", "overlap", "missing_empty", "row_count", "raw_checksum", "missing_artifact"])
def test_rpc_export_requires_every_chunk_including_empty_ones(tmp_path, case):
    path, sidecar, manifest = event_export(tmp_path)
    if case == "gap":
        manifest["chunks"][1]["from_block"] = 103
    elif case == "overlap":
        manifest["chunks"][1]["from_block"] = 101
    elif case == "missing_empty":
        manifest["chunks"].pop()
    elif case == "row_count":
        manifest["chunks"][1]["row_count"] = 1
    elif case == "raw_checksum":
        manifest["chunks"][0]["raw_logs_sha256"] = "0" * 64
    else:
        (tmp_path / manifest["chunks"][0]["evidence_path"]).unlink()
    write_json(sidecar, manifest)
    with pytest.raises(DataContractError):
        load_rpc_export(path, sidecar)


def test_rpc_export_decodes_raw_instead_of_trusting_table(tmp_path):
    path, sidecar, manifest = event_export(tmp_path)
    from lp_pilot.data import read_rows
    rows = read_rows(path)
    rows[0]["amount0"] = "9"
    write_table(path, rows)
    manifest["sha256"] = sha256(path)
    write_json(sidecar, manifest)
    with pytest.raises(DataContractError, match="differs from archived raw log"):
        load_rpc_export(path, sidecar)


def test_empty_rpc_export_is_complete_without_invented_events(tmp_path):
    path, sidecar, _ = event_export(tmp_path, empty=True)
    events, manifest = load_rpc_export(path, sidecar)
    assert events == []
    assert manifest["validated_row_count"] == 0


def test_full_snapshot_checks_all_bitmap_and_tick_results(tmp_path):
    path, snapshot, _, _, _ = snapshot_export(tmp_path)
    result = validate_rpc_snapshot(snapshot, path, root=tmp_path, expected_block=100)
    assert result["validated_bitmap_words"] == 694
    assert result["validated_initialized_ticks"] == 2
    with pytest.raises(DataContractError, match="first processed block"):
        validate_rpc_snapshot(snapshot, path, root=tmp_path, expected_block=101)


@pytest.mark.parametrize("case", ["missing_word", "missing_tick", "tick_value", "global", "wrong_block_pin", "missing_manifest"])
def test_snapshot_proof_cannot_be_replaced_with_declarations(tmp_path, case):
    path, snapshot, evidence_path, evidence, manifest = snapshot_export(tmp_path)
    if case == "missing_word":
        del evidence["calls"]["tickBitmap"]["346"]
    elif case == "missing_tick":
        del snapshot["ticks"]["10"]
    elif case == "tick_value":
        snapshot["ticks"]["-10"]["fee_growth_outside0_x128"] = "4"
    elif case == "global":
        snapshot["fee_growth_global1_x128"] = "201"
    elif case == "wrong_block_pin":
        evidence["calls"]["slot0"]["params"][1] = "latest"
    else:
        path.with_name(path.name + ".manifest.json").unlink()
    write_json(path, snapshot)
    write_json(evidence_path, evidence)
    if case != "missing_manifest":
        write_json(path.with_name(path.name + ".manifest.json"), {**manifest, "snapshot_sha256": sha256(path), "evidence_sha256": sha256(evidence_path)})
    with pytest.raises(DataContractError):
        validate_rpc_snapshot(snapshot, path, root=tmp_path, expected_block=100)


def checkpoint_index(tmp_path, evidence_path, **extra):
    write_json(tmp_path / "checkpoints.json", {"checkpoints": [{"source_kind": "historical_rpc", "block_number": 100,
        "evidence_path": evidence_path.name, "evidence_sha256": sha256(evidence_path), **extra}]})


def test_checkpoint_compares_all_five_decoded_fields_with_zero_tolerance(tmp_path):
    _, snapshot, evidence_path, _, _ = snapshot_export(tmp_path)
    checkpoint_index(tmp_path, evidence_path, sqrt_price_x96="fabricated index value")
    cfg = config(tmp_path)
    check = _checkpoint_checks(snapshot, [], cfg)
    assert check.status == "PASS"
    assert len(cfg["_checkpoint_comparisons"]) == 5
    assert all(row["difference"] == "0" for row in cfg["_checkpoint_comparisons"])
    assert "same RPC provider" in check.detail


@pytest.mark.parametrize("field", ["sqrt_price_x96", "tick", "liquidity", "fee_growth_global0_x128", "fee_growth_global1_x128"])
def test_checkpoint_reports_actual_protocol_integer_mismatches(tmp_path, field):
    _, snapshot, evidence_path, _, _ = snapshot_export(tmp_path)
    checkpoint_index(tmp_path, evidence_path)
    cfg = config(tmp_path)
    snapshot[field] = str(int(snapshot[field]) + 1)
    check = _checkpoint_checks(snapshot, [], cfg)
    assert check.status == "FAIL"
    result = next(row for row in cfg["_checkpoint_comparisons"] if row["field"] == field)
    assert result["difference"] == "1"
    assert field in check.detail


def test_checkpoint_rejects_tampered_raw_artifacts(tmp_path):
    _, snapshot, evidence_path, _, _ = snapshot_export(tmp_path)
    checkpoint_index(tmp_path, evidence_path)
    evidence_path.write_text("{}")
    assert _checkpoint_checks(snapshot, [], config(tmp_path)).status == "BLOCKED"


def test_real_rpc_run_imports_evidence_but_does_not_fabricate_full_success(tmp_path, monkeypatch):
    event_export(tmp_path)
    _, _, evidence_path, _, _ = snapshot_export(tmp_path)
    checkpoint_index(tmp_path, evidence_path)
    monkeypatch.setattr("lp_pilot.pipeline.required_interval", lambda _: (1000, 1036))
    result = run(config(tmp_path), "real")
    assert result["status"] == "BLOCKED"
    assert result["rows"]["events"] == 1
    assert result["empirical_end_to_end"] is False
    checks = {row["name"]: row for row in json.loads((Path(result["output"]) / "manifest.json").read_text())["checks"]}
    assert checks["initialization"]["status"] == "PASS"
    assert checks["reconstruction"]["status"] == "PASS"
    assert checks["coverage"]["status"] == "BLOCKED"  # Reference prices are absent.
    assert checks["position_fees"]["status"] == "BLOCKED"
    assert "On-chain source: Ethereum RPC" in Path(result["report"]).read_text()


@pytest.mark.parametrize("case", ["synthetic_provenance", "labelled_synthetic_provenance", "missing_manifest", "invalid_tick_evidence"])
def test_real_replay_requires_evidenced_historical_initialization(tmp_path, monkeypatch, case):
    event_export(tmp_path)
    snapshot_path, snapshot, evidence_path, evidence, manifest = snapshot_export(tmp_path)
    checkpoint_index(tmp_path, evidence_path)
    sidecar = snapshot_path.with_name(snapshot_path.name + ".manifest.json")
    if case in {"synthetic_provenance", "labelled_synthetic_provenance"}:
        snapshot["provenance"] = "synthetic" if case == "synthetic_provenance" else "labelled synthetic unit fixture"
        write_json(snapshot_path, snapshot)
        manifest["snapshot_sha256"] = sha256(snapshot_path)
        write_json(sidecar, manifest)
    elif case == "missing_manifest":
        sidecar.unlink()
    else:
        evidence["calls"]["ticks"]["10"]["result"] = encode(101, -100, 3, 4, 0, 0, 0, 1)
        write_json(evidence_path, evidence)
        manifest["evidence_sha256"] = sha256(evidence_path)
        write_json(sidecar, manifest)
    monkeypatch.setattr("lp_pilot.pipeline.required_interval", lambda _: (1000, 1036))
    result = run(config(tmp_path), "real")
    assert result["status"] == "BLOCKED"
    assert result["rows"]["events"] == 1
    assert result["rows"]["states"] == result["rows"]["swap_segments"] == 0
    output = Path(result["output"])
    assert not (output / "final_snapshot.json").exists()
    checks = {row["name"]: row for row in json.loads((output / "manifest.json").read_text())["checks"]}
    assert checks["initialization"]["status"] == checks["reconstruction"]["status"] == "BLOCKED"
    from lp_pilot.storage import read_table
    assert all(row["fees0_usdc"] is None and row["fees1_weth"] is None and row["y_usdc"] is None
               for row in read_table(output / "outcomes.parquet"))


def test_real_validated_checkpoint_omits_pure_replay_placeholder(tmp_path, monkeypatch):
    event_export(tmp_path)
    _, _, evidence_path, _, _ = snapshot_export(tmp_path)
    checkpoint_index(tmp_path, evidence_path)
    monkeypatch.setattr("lp_pilot.pipeline.required_interval", lambda _: (1000, 1036))
    result = run(config(tmp_path), "real")
    checks = {row["name"]: row for row in json.loads((Path(result["output"]) / "manifest.json").read_text())["checks"]}
    assert "replay_independent_historical_checkpoints" not in checks
    assert checks["reconstruction"]["status"] == "PASS"


def test_synthetic_run_retains_missing_independent_checkpoint_status(tmp_path):
    result = run(config(tmp_path), "synthetic")
    checks = {row["name"]: row for row in json.loads((Path(result["output"]) / "manifest.json").read_text())["checks"]}
    assert checks["replay_independent_historical_checkpoints"]["status"] == "BLOCKED"
    assert result["rows"]["states"] > 0
    assert result["empirical_end_to_end"] is False

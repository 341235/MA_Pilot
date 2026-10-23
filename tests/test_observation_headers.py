"""Raw header evidence, bounded supplementation and unchanged reception ages."""
import hashlib
import json

import pytest

from lp_pilot import observation_headers as observations
from lp_pilot.data import DataContractError, POOL
from lp_pilot.rpc import RpcClient
from lp_pilot.storage import sha256, write_json, write_table


def block(number):
    return {"number": hex(number), "timestamp": hex(100 + 12 * number),
            "hash": "0x" + format(number + 1, "064x"),
            "parentHash": "0x" + format(number, "064x")}


def save_header(cache, block_number, **changes):
    path = cache / f"header_{block_number}.json"
    write_json(path, {"method": "eth_getBlockByNumber", "params": [hex(block_number), False],
                      "result": {**block(block_number), **changes}, "retrieved_at": "2026-10-22T00:00:00Z"})
    return path


@pytest.fixture
def source(tmp_path):
    cache = tmp_path / "cache"
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    cache.mkdir()
    headers = []
    for number in (0, 99, 100):
        save_header(cache, number)
        raw = block(number)
        headers.append({"block_number": number, "timestamp": int(raw["timestamp"], 16), "block_hash": raw["hash"]})
    events = inputs / "events.parquet"
    write_table(events, [], empty_columns=("timestamp", "block_number"))
    chunks = []
    for first in range(0, 100, 10):
        params = [{"address": POOL, "fromBlock": hex(first), "toBlock": hex(first + 9)}]
        path = cache / f"logs_{first}.json"
        write_json(path, {"method": "eth_getLogs", "params": params, "result": []})
        chunks.append({"from_block": first, "to_block": first + 9, "row_count": 0,
                       "raw_logs_sha256": hashlib.sha256(b"[]").hexdigest(),
                       "evidence_path": str(path), "evidence_sha256": sha256(path)})
    manifest = {"source": "ethereum_rpc", "chain_id": 1, "pool_address": POOL,
                "source_row_count": 0, "normalized_row_count": 0, "coverage_start": 100,
                "coverage_end": 1300, "block_start": 0, "block_end_exclusive": 100,
                "sha256": sha256(events), "chunks": chunks, "boundary_headers": headers,
                "block_headers": headers, "coverage_complete": True, "retrieved_at": "2026-10-22T00:00:00Z"}
    write_json(inputs / "export_manifest.json", manifest)
    cfg = {"_root": str(tmp_path), "pool": {"address": POOL},
           "pilot": {"start": "1970-01-01T00:10:00Z", "end": "1970-01-01T00:11:00Z",
                     "decision_seconds": 60, "holding_seconds": 60},
           "timing": {"block_delay_seconds": 12, "execution_delay_seconds": 12,
                      "benchmark_seconds": 60, "max_pool_age_seconds": 120},
           "data": {"events_path": "inputs/events.parquet", "export_manifest_path": "inputs/export_manifest.json"},
           "network": {"cache_dir": "cache"}}
    return cfg, tmp_path, cache


class Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def iter_content(self, _size):
        yield json.dumps(self.body).encode()


class Session:
    def __init__(self):
        self.numbers = []

    def post(self, _endpoint, *, json, **kwargs):
        assert json["method"] == "eth_getBlockByNumber"
        assert json["params"][1] is False
        number = int(json["params"][0], 16)
        assert 0 <= number <= 100
        self.numbers.append(number)
        return Response({"jsonrpc": "2.0", "id": json["id"], "result": block(number)})


def install_client(monkeypatch, cache, *, max_requests=1000):
    session = Session()
    client = RpcClient("https://example.invalid/secret", cache_dir=cache, session=session,
                       max_requests=max_requests, max_bytes=100_000_000,
                       max_compute_units=100_000, retries=0)
    monkeypatch.setattr(observations, "make_rpc_client", lambda cfg: client)
    return session, client


def load_index(root):
    return observations.load_observation_headers(root / "inputs/observation_headers.json", root=root,
                                                 export_manifest_path=root / "inputs/export_manifest.json")


def test_cache_only_preserves_missing_observations_and_never_reads_endpoint(source, monkeypatch):
    cfg, root, _ = source
    monkeypatch.setattr(observations, "make_rpc_client", lambda cfg: pytest.fail("Offline preparation used the endpoint"))
    report = observations.prepare_observation_headers(cfg)
    assert report["status"] == "BLOCKED"
    assert report["summary"]["observation_header_count"] == 2
    assert report["summary"]["blocks_without_observation_headers"] == 98
    assert report["usage"]["logical_requests"] == 0
    assert any(row["status"] == "BLOCKED" for row in report["target_checks"])
    headers, summary = load_index(root)
    assert [row["block_number"] for row in headers] == [0, 99]
    assert summary["raw_log_reconciliation"] == "verified"


def test_sparse_cached_raw_headers_outside_original_manifest_are_recovered(source):
    cfg, root, cache = source
    for number in (36, 40, 42, 47):
        save_header(cache, number)
    report = observations.prepare_observation_headers(cfg)
    assert report["status"] == "PASS"
    assert report["summary"]["blocks_without_observation_headers"] == 94
    headers, _ = load_index(root)
    assert 40 in [row["block_number"] for row in headers]
    longer = next(row for row in report["target_checks"] if row["kind"] == "decision_delay_60s")
    assert longer["observation_timestamp"] == 532
    assert longer["observation_age_seconds"] == 68
    assert longer["observation_age_seconds"] != 8  # Reception delay cannot rejuvenate the observation.


def test_targeted_header_binary_search_uses_existing_limits_and_preserves_originals(source, monkeypatch):
    cfg, root, cache = source
    original = {path: sha256(path) for path in cache.glob("*.json")}
    export_hash = sha256(root / "inputs/export_manifest.json")
    session, client = install_client(monkeypatch, cache)
    report = observations.prepare_observation_headers(cfg, allow_network=True)
    assert report["status"] == "PASS"
    assert 0 < len(session.numbers) < 25
    assert all(0 < number < 99 for number in session.numbers)
    assert client.budget.max_requests == 512
    assert client.budget.max_bytes == 64_000_000
    assert client.max_compute_units == 10_240
    assert {path: sha256(path) for path in original} == original
    assert sha256(root / "inputs/export_manifest.json") == export_hash
    assert "secret" not in (root / "inputs/observation_headers.json").read_text()
    _, summary = load_index(root)
    assert summary["observation_header_count"] > 2
    for row in report["target_checks"]:
        assert row["observation_timestamp"] + row["delay_seconds"] <= row["query_time"]
        assert row["observation_age_seconds"] == row["query_time"] - row["observation_timestamp"]


def test_smaller_existing_budget_is_not_raised_and_partial_evidence_stays_blocked(source, monkeypatch):
    cfg, _, cache = source
    session, client = install_client(monkeypatch, cache, max_requests=1)
    report = observations.prepare_observation_headers(cfg, allow_network=True)
    assert report["status"] == "BLOCKED"
    assert client.budget.max_requests == 1
    assert len(session.numbers) == report["usage"]["logical_requests"] == 1
    assert "budget exhausted" in report["error"]


def test_repreparation_replaces_derived_index_without_archive(source):
    cfg, root, cache = source
    first = observations.prepare_observation_headers(cfg)
    save_header(cache, 40)
    second = observations.prepare_observation_headers(cfg)
    saved = json.loads((root / first["path"]).read_text())
    assert len(saved["headers"]) == second["summary"]["observation_header_count"] + 1
    assert any(row["block_number"] == 40 for row in saved["headers"])
    assert saved["status"] == second["status"]
    assert not (root / "inputs/observation_header_history").exists()


@pytest.mark.parametrize("problem", ["missing", "conflict", "number", "parent"])
def test_missing_conflicting_or_wrong_raw_headers_reject_the_index(source, problem):
    cfg, _, cache = source
    if problem == "missing":
        (cache / "header_99.json").unlink()
    elif problem == "conflict":
        path = cache / "duplicate.json"
        original = json.loads((cache / "header_99.json").read_text())
        original["result"]["hash"] = "0x" + "ff" * 32
        write_json(path, original)
    elif problem == "number":
        save_header(cache, 99, number=hex(98))
    else:
        save_header(cache, 100, parentHash="0x" + "ff" * 32)
    with pytest.raises(DataContractError):
        observations.prepare_observation_headers(cfg)


@pytest.mark.parametrize("tamper", ["raw", "indexed_time", "export", "chunk_gap"])
def test_loader_revalidates_raw_checksums_and_original_complete_log_coverage(source, tamper):
    cfg, root, cache = source
    observations.prepare_observation_headers(cfg)
    index_path = root / "inputs/observation_headers.json"
    index = json.loads(index_path.read_text())
    if tamper == "raw":
        save_header(cache, 99, timestamp=hex(1200))
    elif tamper == "indexed_time":
        index["headers"][0]["timestamp"] += 1
        write_json(index_path, index)
    else:
        path = root / "inputs/export_manifest.json"
        manifest = json.loads(path.read_text())
        manifest["chunks"][0]["from_block"] = 1
        write_json(path, manifest)
        if tamper == "chunk_gap":
            index["export_manifest_sha256"] = sha256(path)
            write_json(index_path, index)
    with pytest.raises(DataContractError):
        load_index(root)

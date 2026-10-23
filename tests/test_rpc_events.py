"""UTC interval boundaries and historical log acquisition."""
from copy import deepcopy
import json

import pytest

from lp_pilot.data import DataContractError, EVENT_TOPICS, POOL
from lp_pilot.rpc import RpcClient, RpcError
from lp_pilot.rpc_events import download_events, find_block_at_or_after


def words(*values):
    return "0x" + "".join(format(value % (1 << 256), "064x") for value in values)


def header(number):
    return {"number": hex(number), "hash": "0x" + format(number + 1000, "064x"),
            "timestamp": hex(100 + number * 12)}


def swap(number, index=0):
    return {"address": POOL, "blockNumber": hex(number), "blockHash": header(number)["hash"],
            "transactionHash": "0x" + format(number + 2000, "064x"),
            "transactionIndex": "0x1", "logIndex": hex(index), "removed": False,
            "topics": [EVENT_TOPICS["Swap"], words(1), words(2)],
            "data": words(2**200, -3, 2**100, 2**100, -123)}


class Chain:
    def __init__(self, logs=(), latest=40, mutate=None):
        self.logs = list(logs)
        self.latest = latest
        self.calls = []
        self.batches = []
        self.mutate = mutate

    def call(self, method, params=None):
        params = params or []
        self.calls.append((method, deepcopy(params)))
        if method == "eth_chainId":
            return "0x1"
        if method == "eth_blockNumber":
            return hex(self.latest)
        if method == "eth_getBlockByNumber":
            return header(int(params[0], 16))
        if method == "eth_getLogs":
            first, last = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
            assert 0 <= last - first < 10
            assert "topics" not in params[0]
            logs = deepcopy([log for log in self.logs if first <= int(log["blockNumber"], 16) <= last])
            return self.mutate(logs) if self.mutate else logs
        raise AssertionError(method)

    def batch_call(self, calls):
        self.batches.append(calls)
        assert len(calls) <= 10
        return [self.call(method, params) for method, params in calls]


@pytest.mark.parametrize(("timestamp", "expected"), [(99, 0), (100, 0), (112, 1), (113, 2), (580, 40)])
def test_timestamp_lower_bound(timestamp, expected):
    chain = Chain()
    assert find_block_at_or_after(chain, timestamp) == expected
    assert len(chain.calls) <= 12


def test_timestamp_search_bounds_must_prove_the_actual_lower_bound():
    chain = Chain()
    assert find_block_at_or_after(chain, 221, low=10, high=20) == 11
    with pytest.raises(RpcError, match="excludes"):
        find_block_at_or_after(chain, 221, low=12, high=20)
    with pytest.raises(RpcError, match="beyond"):
        find_block_at_or_after(chain, 581)
    with pytest.raises(RpcError, match="interval"):
        find_block_at_or_after(chain, 221, low=20, high=10)


def test_full_bounded_coverage_keeps_raw_precision_and_half_open_edges():
    chain = Chain([swap(32), swap(31), swap(20), swap(11), swap(10)])
    updates = []
    events, manifest = download_events(chain, 221, 484, progress=updates.append)
    assert [row["block_number"] for row in events] == [11, 20, 31]
    assert events[0]["amount0"] == 2**200
    assert events[0]["amount1"] == -3 and events[0]["tick"] == -123
    assert events[0]["raw_data"] == swap(11)["data"]
    assert events[0]["topics"] == swap(11)["topics"]
    assert manifest["block_start"] == 11 and manifest["block_end_exclusive"] == 32
    assert [(chunk["from_block"], chunk["to_block"], chunk["row_count"]) for chunk in manifest["chunks"]] == [(11, 20, 2), (21, 30, 0), (31, 31, 1)]
    assert manifest["source_row_count"] == manifest["normalized_row_count"] == manifest["hash_checks"] == 3
    assert manifest["coverage_complete"] is True
    assert "independent completeness is not established" in manifest["completeness_caveat"]
    assert updates[-1]["completed_chunks"] == updates[-1]["total_chunks"] == 3
    assert updates[-1]["raw_log_count"] == 3
    assert {row["block_number"] for row in manifest["boundary_headers"]} == {10, 11, 31, 32}


def test_interval_containing_no_block_has_empty_proven_coverage():
    chain = Chain()
    events, manifest = download_events(chain, 221, 222)
    assert events == [] and manifest["chunks"] == []
    assert manifest["block_start"] == manifest["block_end_exclusive"] == 11
    assert manifest["source_row_count"] == 0
    assert not any(method == "eth_getLogs" for method, _ in chain.calls)


@pytest.mark.parametrize("changes", [{"address": "0x" + "ff" * 20}, {"blockHash": "0x" + "ff" * 32}, {"removed": True}, {"blockNumber": "0x29"}])
def test_log_header_address_and_reorg_validation(changes):
    def mutate(logs):
        return [{**log, **changes} for log in logs]
    with pytest.raises((RpcError, DataContractError)):
        download_events(Chain([swap(11)], mutate=mutate), 221, 250)


def test_duplicate_log_identity_is_rejected_instead_of_removed():
    with pytest.raises(DataContractError, match="Duplicate"):
        download_events(Chain([swap(11), swap(11)]), 221, 250)


def test_unknown_topics_are_preserved_for_explicit_pipeline_blocking():
    log = swap(11)
    log["topics"] = ["0x" + "aa" * 32]
    log["data"] = "0x"
    events, manifest = download_events(Chain([log]), 221, 250)
    assert events[0]["event_type"] == "Unknown"
    assert manifest["unknown_event_count"] == 1


@pytest.mark.parametrize("chunk_blocks", [0, 11, True, 1.5])
def test_chunk_budget_validation_happens_before_network(chunk_blocks):
    chain = Chain()
    with pytest.raises(RpcError, match="chunk size"):
        download_events(chain, 221, 250, chunk_blocks=chunk_blocks)
    assert chain.calls == []


def test_provider_failure_does_not_trigger_unbounded_split_or_retry():
    def fail(_logs):
        raise RpcError("Provider rejected bounded request")
    chain = Chain(mutate=fail)
    with pytest.raises(RpcError, match="Provider rejected"):
        download_events(chain, 221, 484)
    assert sum(method == "eth_getLogs" for method, _ in chain.calls) == 1


class Response:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        pass

    def iter_content(self, _size):
        yield json.dumps(self.value).encode()


class Session:
    def __init__(self, chain):
        self.chain = chain
        self.requests = 0

    def post(self, _url, **kwargs):
        self.requests += 1
        def execute(call):
            return {"jsonrpc": "2.0", "id": call["id"],
                    "result": self.chain.call(call["method"], call["params"])}
        payload = kwargs["json"]
        return Response([execute(call) for call in payload] if isinstance(payload, list) else execute(payload))


def test_immutable_cache_reuses_all_log_windows_on_resume(tmp_path):
    session = Session(Chain([swap(11), swap(31)]))
    client = RpcClient("https://example.invalid/private-key", session=session, cache_dir=tmp_path, max_requests=100)
    first_events, first_manifest = download_events(client, 221, 484)
    first_log_queries = sum(method == "eth_getLogs" for method, _ in session.chain.calls)
    second_events, second_manifest = download_events(client, 221, 484)
    assert first_log_queries == 3
    assert sum(method == "eth_getLogs" for method, _ in session.chain.calls) == 3
    assert first_events == second_events
    assert first_manifest["chunks"] == second_manifest["chunks"]
    assert all("private-key" not in path.read_text() for path in tmp_path.glob("*.json"))

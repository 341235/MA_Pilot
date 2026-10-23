"""Synthetic RPC transport for batching and budget checks."""
import json

import pytest
import requests

from lp_pilot.rpc import RpcClient, RpcError


class Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield json.dumps(self.body).encode()


class Session:
    def __init__(self, transform):
        self.transform = transform
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(kwargs)
        return Response(self.transform(kwargs["json"]))


def test_out_of_order_batch_results_are_matched_by_id_and_cached(tmp_path):
    session = Session(lambda body: [{"id": r["id"], "result": r["params"][0]} for r in reversed(body)])
    client = RpcClient("https://private.invalid/secret", cache_dir=tmp_path, session=session,
                       max_requests=2, max_compute_units=40)
    calls = [("eth_getBlockByNumber", [hex(n), False]) for n in (100, 101)]
    assert client.batch_call(calls) == ["0x64", "0x65"]
    assert client.batch_call(calls) == ["0x64", "0x65"]
    assert len(session.calls) == 1
    assert client.statistics()["estimated_compute_units"] == 40
    assert client.statistics()["cache_hits"] == 2
    evidence = client.evidence_reference(*calls[0])
    assert len(evidence["sha256"]) == 64
    assert "secret" not in json.dumps(evidence)


@pytest.mark.parametrize("body", [[], [{"id": 0, "result": 1}],
    [{"id": 0, "result": 1}, {"id": 0, "result": 2}],
    [{"id": 0, "result": 1}, {"id": 20, "result": 2}],
    [{"id": False, "result": 1}, {"id": 1, "result": 2}]])
def test_incomplete_or_ambiguous_batch_response_fails(body):
    client = RpcClient("https://private.invalid", session=Session(lambda _: body))
    with pytest.raises(RpcError):
        client.batch_call([("eth_chainId", []), ("eth_chainId", [])])


def test_cu_budget_is_charged_per_method_and_rejects_batch_before_sending():
    session = Session(lambda body: [])
    client = RpcClient("https://private.invalid", session=session, max_compute_units=51)
    with pytest.raises(RpcError, match="compute-unit budget"):
        client.batch_call([("eth_call", [{}, "0x64"])] * 2)
    assert session.calls == []
    assert client.budget.used_requests == 0


def test_fresh_headers_bypass_cache_and_blockhash_calls_are_cached(tmp_path):
    session = Session(lambda body: {"result": "0x1"})
    client = RpcClient("https://private.invalid", cache_dir=tmp_path, session=session)
    params = [{"to": "0x00", "data": "0x01"}, {"blockHash": "0x" + "11" * 32, "requireCanonical": True}]
    client.call("eth_call", params)
    client.call("eth_call", params)
    client.call("eth_call", params, use_cache=False)
    assert len(session.calls) == 2


def test_batch_provider_error_is_sanitized():
    client = RpcClient("https://private.invalid/secret", session=Session(lambda body: [
        {"id": 0, "error": {"code": -32000, "message": "private-secret-url"}}]))
    with pytest.raises(RpcError) as error:
        client.batch_call([("eth_chainId", [])])
    assert "secret" not in str(error.value)


def test_auth_failure_is_not_retried():
    class FailingSession:
        calls = 0
        def post(self, *args, **kwargs):
            self.calls += 1
            response = requests.Response()
            response.status_code = 401
            raise requests.HTTPError("secret", response=response)
    session = FailingSession()
    client = RpcClient("https://private.invalid/secret", session=session, retries=2)
    with pytest.raises(RpcError, match="HTTP 401"):
        client.call("eth_chainId")
    assert session.calls == 1


def test_single_response_id_must_match_its_request():
    client = RpcClient("https://private.invalid", session=Session(lambda body: {"id": 99, "result": "0x1"}))
    with pytest.raises(RpcError, match="response ID"):
        client.call("eth_chainId")

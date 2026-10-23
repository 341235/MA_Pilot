"""RPC client tests with synthetic responses."""
import json

import pytest
import requests

from lp_pilot.data import POOL
from lp_pilot.rpc import RpcClient, RpcError, SELECTORS, USDC, WETH, verify_pool_identity


def encode(*values):
    return "0x" + "".join(format(value % (1 << 256), "064x") for value in values)


class Response:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield json.dumps(self.value).encode()


class Session:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(kwargs)
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return Response({"result": value})


def test_historical_cache_current_not_cached_and_budget(tmp_path):
    session = Session([encode(12), encode(13), encode(14)])
    client = RpcClient("https://example.invalid/private-secret", session=session, cache_dir=tmp_path, max_requests=3)
    assert client.eth_call(POOL, SELECTORS["liquidity"], 100) == [12]
    assert client.eth_call(POOL, SELECTORS["liquidity"], 100) == [12]
    assert client.eth_call(POOL, SELECTORS["liquidity"]) == [13]
    assert client.eth_call(POOL, SELECTORS["liquidity"]) == [14]
    with pytest.raises(RpcError, match="budget"):
        client.call("eth_chainId")
    assert len(session.calls) == 3
    assert all(call["timeout"] == 10 for call in session.calls)
    assert "private-secret" not in next(tmp_path.iterdir()).read_text()


def test_pool_identity_from_contract_data():
    values = ["0x1", encode(int(USDC, 16)), encode(int(WETH, 16)), encode(500), encode(10), encode(6), encode(18)]
    session = Session(values)
    result = verify_pool_identity(RpcClient("https://example.invalid", session=session))
    assert result["status"] == "PASS"
    assert result["decimals0"] == 6 and result["decimals1"] == 18
    assert session.calls[1]["json"]["params"][0]["data"] == SELECTORS["token0"]


def test_log_bounds_and_read_only_guard():
    client = RpcClient("https://example.invalid", session=Session([]))
    for method, params in [("eth_sendRawTransaction", ["0x01"]), ("eth_getLogs", [{"fromBlock": "earliest", "toBlock": "latest"}]), ("eth_getLogs", [{"fromBlock": "0x1", "toBlock": "0x1000"}])]:
        with pytest.raises(RpcError):
            client.call(method, params)
    assert client.budget.used_requests == 0


def test_errors_are_sanitized_and_retries_bounded():
    session = Session([requests.ConnectionError("https://secret-key.example"), requests.ConnectionError("secret")])
    client = RpcClient("https://secret-key.example", session=session, retries=1, max_requests=2)
    with pytest.raises(RpcError) as raised:
        client.call("eth_chainId")
    assert "secret" not in str(raised.value)
    assert len(session.calls) == 2


def test_signed_rpc_arguments():
    session = Session([encode(1)])
    client = RpcClient("https://example.invalid", session=session)
    client.eth_call(POOL, SELECTORS["ticks"], 100, -10)
    assert session.calls[0]["json"]["params"][0]["data"].endswith(format((1 << 256) - 10, "064x"))

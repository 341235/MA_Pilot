"""Synthetic RPC snapshots for completeness checks."""
from copy import deepcopy

import pytest

from lp_pilot.data import POOL
from lp_pilot.replay import PoolState
from lp_pilot.rpc import RpcError, SELECTORS, USDC, WETH
from lp_pilot.rpc_snapshot import bitmap_word_bounds, fetch_full_snapshot, fetch_state_checkpoint
from lp_pilot.uniswap import get_sqrt_ratio_at_tick


def encode(*values):
    return "0x" + "".join(format(value % (1 << 256), "064x") for value in values)


class HistoricalClient:
    def __init__(self):
        self.header = {"number": "0x64", "hash": "0x" + "11" * 32,
                       "timestamp": "0x659f3200", "stateRoot": "0x" + "22" * 32}
        self.chain = "0x1"
        self.calls = []
        self.batches = []
        self.values = {
            "slot0": encode(get_sqrt_ratio_at_tick(0), 0, 0, 1, 1, 0x54, 1),
            "liquidity": encode(100), "feeGrowthGlobal0X128": encode(2**220),
            "feeGrowthGlobal1X128": encode(2**221), "tickSpacing": encode(10),
            "fee": encode(500), "token0": encode(int(USDC, 16)), "token1": encode(int(WETH, 16)),
        }
        self.bitmaps = {-1: 1 << 255, 0: 1 << 1}
        self.ticks = {-10: encode(100, 100, 12, 13, -1, 0, 0, 1),
                      10: encode(100, -100, 14, 15, 1, 0, 0, 1)}
        self.change_header = False
        self.shorten_batch = False

    def call(self, method, params=None, **kwargs):
        self.calls.append((method, params, kwargs))
        if method == "eth_chainId":
            return self.chain
        assert method == "eth_getBlockByNumber"
        assert params == ["0x64", False]
        assert kwargs == {"use_cache": False}
        header = deepcopy(self.header)
        if self.change_header and sum(call[0] == method for call in self.calls) > 1:
            header["hash"] = "0x" + "33" * 32
        return header

    def batch_call(self, batch):
        self.batches.append(batch)
        assert 0 < len(batch) <= 10
        output = []
        for method, params in batch:
            assert method == "eth_call"
            target, block = params
            assert target["to"] == POOL
            assert block == {"blockHash": self.header["hash"], "requireCanonical": True}
            data = target["data"]
            name = next(name for name, selector in SELECTORS.items() if selector == data[:10])
            if name in ("tickBitmap", "ticks"):
                argument = int(data[10:], 16)
                if argument >= 1 << 255:
                    argument -= 1 << 256
                result = encode(self.bitmaps.get(argument, 0)) if name == "tickBitmap" else self.ticks.get(argument, encode(0, 0, 0, 0, 0, 0, 0, 0))
            else:
                result = self.values[name]
            output.append(result)
        return output[:-1] if self.shorten_batch else output


def test_full_snapshot_queries_entire_protocol_bitmap_and_proves_liquidity():
    client = HistoricalClient()
    snapshot = fetch_full_snapshot(client, 100)
    assert bitmap_word_bounds() == (-347, 346)
    assert set(map(int, snapshot["bitmap_words"])) == set(range(-347, 347))
    assert len(snapshot["bitmap_words"]) == 694
    assert snapshot["ticks_complete"] is True
    assert set(snapshot["ticks"]) == {"-10", "10"}
    assert snapshot["ticks"]["10"]["liquidity_net"] == "-100"
    assert snapshot["fee_protocol0"] == 4 and snapshot["fee_protocol1"] == 5
    assert snapshot["fee_growth_global0_x128"] == str(2**220)
    assert PoolState.from_snapshot(snapshot).liquidity == 100
    evidence = snapshot["evidence"]
    assert evidence["status"] == "PASS"
    assert evidence["source_kind"] == "historical_rpc" and evidence["chain_id"] == 1
    assert evidence["block_header_before"] == evidence["block_header_after"]
    assert len(evidence["calls"]["tickBitmap"]) == 694
    assert evidence["calls"]["ticks"]["10"]["result"] == client.ticks[10]
    assert evidence["calls"]["tickBitmap"]["-347"]["params"][0]["data"].endswith(format((1 << 256) - 347, "064x"))
    assert evidence["bitmap_proof"]["net_liquidity_sum"] == "0"
    assert evidence["bitmap_proof"]["active_liquidity_sum"] == "100"
    assert evidence["bitmap_proof"]["queried_tick_count"] == 2


def test_all_empty_words_are_recorded_without_inventing_ticks():
    client = HistoricalClient()
    client.bitmaps = {}
    client.values["liquidity"] = encode(0)
    snapshot = fetch_full_snapshot(client, 100, batch_size=7)
    assert snapshot["ticks"] == {} and snapshot["ticks_complete"] is True
    assert len(snapshot["bitmap_words"]) == 694
    assert all(value == "0" for value in snapshot["bitmap_words"].values())


def test_missing_batch_result_fails_instead_of_becoming_zero():
    client = HistoricalClient()
    client.shorten_batch = True
    with pytest.raises(RpcError, match="missing or extra"):
        fetch_full_snapshot(client, 100)


@pytest.mark.parametrize("result", [None, "0x", "0x00", "0x" + "z" * 64, "0x+" + "0" * 63, "0x" + "0_" * 32])
def test_malformed_or_missing_contract_result_fails(result):
    client = HistoricalClient()
    client.values["liquidity"] = result
    with pytest.raises(RpcError, match="ABI shape|not hexadecimal"):
        fetch_full_snapshot(client, 100)


@pytest.mark.parametrize("word,bit", [(-347, 0), (346, 255)])
def test_out_of_range_boundary_bits_are_rejected(word, bit):
    client = HistoricalClient()
    client.bitmaps[word] = 1 << bit
    with pytest.raises(RpcError, match="outside usable tick range"):
        fetch_full_snapshot(client, 100)


@pytest.mark.parametrize("tick_result", [encode(0, 0, 0, 0, 0, 0, 0, 0), encode(100, 100, 0, 0, 0, 0, 0, 0), encode(100, 100, 0, 0, 0, 0, 0, 2)])
def test_set_bits_require_valid_initialized_nonzero_ticks(tick_result):
    client = HistoricalClient()
    client.ticks[-10] = tick_result
    with pytest.raises(RpcError, match="initialized|boolean"):
        fetch_full_snapshot(client, 100)


def test_nonzero_net_liquidity_sum_cannot_be_labeled_complete():
    client = HistoricalClient()
    client.ticks[10] = encode(100, -99, 14, 15, 0, 0, 0, 1)
    with pytest.raises(RpcError, match="zero net liquidity sum"):
        fetch_full_snapshot(client, 100)


def test_active_liquidity_mismatch_cannot_be_labeled_complete():
    client = HistoricalClient()
    client.values["liquidity"] = encode(99)
    with pytest.raises(RpcError, match="active liquidity disagrees"):
        fetch_full_snapshot(client, 100)


def test_canonical_header_change_rejects_snapshot():
    client = HistoricalClient()
    client.change_header = True
    with pytest.raises(RpcError, match="canonical block changed"):
        fetch_full_snapshot(client, 100)


def test_tick_limit_stops_before_fetching_any_tick_records():
    client = HistoricalClient()
    with pytest.raises(RpcError, match="tick limit exceeded"):
        fetch_full_snapshot(client, 100, max_initialized_ticks=1)
    assert not any(request[1][0]["data"].startswith(SELECTORS["ticks"]) for batch in client.batches for request in batch)


@pytest.mark.parametrize("name,value", [("fee", 3000), ("tickSpacing", 60), ("token0", int(WETH, 16))])
def test_historical_pool_identity_must_match(name, value):
    client = HistoricalClient()
    client.values[name] = encode(value)
    with pytest.raises(RpcError, match="immutable identity mismatch"):
        fetch_full_snapshot(client, 100)


def test_wrong_chain_fails_before_reading_contract_state():
    client = HistoricalClient()
    client.chain = "0x89"
    with pytest.raises(RpcError, match="Ethereum mainnet"):
        fetch_full_snapshot(client, 100)
    assert client.batches == []


def test_selected_checkpoint_includes_zero_uninitialized_tick_without_claiming_completeness():
    client = HistoricalClient()
    checkpoint = fetch_state_checkpoint(client, 100, selected_ticks=[-10, 20])
    assert checkpoint["ticks_complete"] is False
    assert checkpoint["ticks"]["20"]["initialized"] is False
    assert checkpoint["ticks"]["20"]["liquidity_gross"] == "0"
    assert checkpoint["ticks"]["-10"]["fee_growth_outside0_x128"] == "12"
    assert checkpoint["evidence"]["calls"]["ticks"]["20"]["result"] == encode(0, 0, 0, 0, 0, 0, 0, 0)
    assert "tickBitmap" not in checkpoint["evidence"]["calls"]
    assert checkpoint["evidence"]["status"] == "PASS"


def test_checkpoint_decodes_negative_current_tick_and_signed_tick_fields():
    client = HistoricalClient()
    client.values["slot0"] = encode(get_sqrt_ratio_at_tick(-5), -5, 0, 1, 1, 0, 1)
    checkpoint = fetch_state_checkpoint(client, 100, selected_ticks=[10])
    assert checkpoint["tick"] == -5
    assert checkpoint["ticks"]["10"]["liquidity_net"] == "-100"


@pytest.mark.parametrize("kwargs", [{"batch_size": 11}, {"batch_size": 0}, {"tick_spacing": 60}, {"max_initialized_ticks": 0}, {"pool_address": "0x" + "00" * 20}])
def test_snapshot_argument_guards(kwargs):
    with pytest.raises(RpcError):
        fetch_full_snapshot(HistoricalClient(), 100, **kwargs)


@pytest.mark.parametrize("ticks", [[-10, -10], [11], [887280], [True]])
def test_checkpoint_tick_argument_guards(ticks):
    with pytest.raises(RpcError):
        fetch_state_checkpoint(HistoricalClient(), 100, selected_ticks=ticks)

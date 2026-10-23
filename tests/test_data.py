"""Synthetic fixtures for input decoding and validation."""
import pyarrow.parquet as pq
import pytest

from lp_pilot.data import (
    DataContractError, EVENT_TOPICS, POOL, RequestBudget, binance_timestamp_seconds,
    decode_pool_log, load_binance_klines, normalize_event,
    order_events, parse_integer, read_rows,
)
from lp_pilot.storage import write_table


def word(value):
    return format(value % (1 << 256), "064x")


def event(**changes):
    return {"block_number": 1, "block_hash": "0x" + "aa" * 32,
            "timestamp": 1705276800, "transaction_hash": "0x" + "bb" * 32,
            "transaction_index": 0, "log_index": 0, "event_type": "Swap",
            "pool_address": POOL, "amount0": 2**255 - 1, "amount1": -10,
            "sqrt_price_x96": 2**159 + 1, "liquidity": 2**127 + 1,
            "tick": 100, **changes}


def test_lossless_integer_roundtrip(tmp_path):
    path = tmp_path / "events.parquet"
    write_table(path, [event()])
    assert pq.read_schema(path).field("sqrt_price_x96").type == __import__("pyarrow").string()
    loaded = normalize_event(read_rows(path)[0])
    assert loaded == event()
    for bad in (float(2**159), "1.0", "1e25", True):
        with pytest.raises(DataContractError):
            parse_integer(bad)


def test_raw_swap_and_mint_decoding():
    raw = {"topics": [EVENT_TOPICS["Swap"], "0x" + word(1), "0x" + word(2)],
           "data": "0x" + "".join(word(value) for value in (2**200, -1, 2**150, 100, -5))}
    decoded = decode_pool_log(raw)
    assert decoded["amount0"] == 2**200
    assert decoded["amount1"] == -1
    assert decoded["tick"] == -5
    mint = {"topics": [EVENT_TOPICS["Mint"], "0x" + word(1), "0x" + word(-10), "0x" + word(20)],
            "data": "0x" + "".join(word(value) for value in (2, 15, 2**250, 3))}
    decoded = decode_pool_log(mint)
    assert decoded["tick_lower"] == -10
    assert decoded["amount0"] == 2**250
    assert decoded["amount"] == 15


def test_fee_relevant_and_unknown_events_are_preserved():
    flash = decode_pool_log({"topics": [EVENT_TOPICS["Flash"], "0x" + word(1), "0x" + word(2)], "data": "0x" + "".join(word(x) for x in [1, 2, 3, 4])})
    assert flash["event_type"] == "Flash" and flash["paid1"] == 4
    fee = decode_pool_log({"topic0": EVENT_TOPICS["SetFeeProtocol"], "data": "0x" + "".join(word(x) for x in [0, 0, 4, 5])})
    assert fee["fee_protocol1_new"] == 5
    assert decode_pool_log({"topics": ["0x" + "ff" * 32], "data": "0x"})["event_type"] == "Unknown"
    with pytest.raises(DataContractError):
        decode_pool_log({"topics": [EVENT_TOPICS["Initialize"]], "data": "0x" + word(1)})


def test_event_order_duplicates_reorg_and_hash_consistency():
    first, last = event(), event(log_index=1)
    assert order_events([last, first]) == [first, last]
    with pytest.raises(DataContractError, match="Duplicate"):
        order_events([first, first])
    with pytest.raises(DataContractError, match="block hash"):
        order_events([first, event(log_index=1, block_hash="0x" + "cc" * 32)])
    with pytest.raises(DataContractError, match="Removed"):
        normalize_event(event(removed=True))
    assert normalize_event(event(removed="false"))["removed"] is False
    with pytest.raises(DataContractError, match="boolean"):
        normalize_event(event(removed="no"))


@pytest.mark.parametrize("scale,last", [(1000, 999), (1000000, 999999)])
def test_binance_ms_us_and_close_availability(tmp_path, scale, last):
    start = 1705276800
    path = tmp_path / "kline.csv"
    values = [start * scale, "2500", "2501", "2499", "2500.5", "10", (start + 59) * scale + last, "25000", "3", "5", "12500", "0"]
    path.write_text(",".join(map(str, values)) + "\n")
    rows = load_binance_klines(path, "ETHUSDT")
    assert rows[0]["available_at"] == start + 60
    assert rows[0]["timestamp"] == start + 60
    assert binance_timestamp_seconds(start * scale) == start
    with pytest.raises(DataContractError):
        binance_timestamp_seconds(start)


def test_request_budget_is_hard_bound():
    budget = RequestBudget(max_requests=1, max_bytes=10)
    budget.claim()
    with pytest.raises(DataContractError, match="request budget"):
        budget.claim()
    with pytest.raises(DataContractError, match="byte budget"):
        budget.consume(11)

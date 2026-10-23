"""Synthetic timestamp and availability cases."""
from lp_pilot.temporal import apply_availability, backward_asof


def test_backward_join_never_backfills_or_uses_unavailable_prices():
    rows = [{"timestamp": 0, "available_at": 0, "price": 10},
            {"timestamp": 9, "available_at": 12, "price": 20},
            {"timestamp": 11, "available_at": 11, "price": 30}]
    assert backward_asof(rows, 10, 20, value_key="price")["value"] == 10
    assert backward_asof(rows, -1, 20)["row"] is None
    assert backward_asof(rows, 10, 5)["flags"] == ["STALE_ASOF_OBSERVATION"]
    assert backward_asof(rows, 10, 20, value_key="price", available_by=12)["value"] == 20


def test_candle_close_and_configured_lag():
    row = apply_availability([{"timestamp": 1705276860, "price_usdc_per_eth": 2500}], 1)[0]
    assert row["timestamp"] == 1705276860
    assert row["available_at"] == 1705276861
    assert backward_asof([row], 1705276859, 120)["row"] is None
    assert backward_asof([row], 1705276860, 120)["row"] is None
    assert backward_asof([row], 1705276861, 120)["row"]
    assert apply_availability([{"timestamp": 1, "available_at": 9}], 5)[0]["available_at"] == 9

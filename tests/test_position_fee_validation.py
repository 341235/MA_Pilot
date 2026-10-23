"""Independent fee arithmetic and the evidence required by endpoint differences."""

from copy import deepcopy
from decimal import Decimal

import pytest

from lp_pilot import position_fee_reference as reference
from lp_pilot.position_fee_reference import (
    fee_growth_inside, interval_claim, keccak256, pending_claim, position_key,
)
from lp_pilot.position_fee_validation import check_applicability, compare_fees
from lp_pilot.uniswap import get_sqrt_ratio_at_tick


Q128 = 1 << 128
UINT256 = 1 << 256
OWNER = "0x" + "12" * 20
OTHER_OWNER = "0x" + "34" * 20
CASE = {"owner": OWNER, "tick_lower": -10, "tick_upper": 10}


def endpoints():
    position = dict(liquidity=100, fee_growth_inside0_last_x128=7,
                    fee_growth_inside1_last_x128=11, tokens_owed0=13, tokens_owed1=17)
    ticks = {tick: dict(initialized=True, liquidity_gross=200) for tick in (-10, 10)}
    start = dict(block_number=100, position=position, ticks=ticks)
    end = deepcopy(start)
    end["block_number"] = 200
    return start, end


def event(kind, *, block=150, log=0, owner=OTHER_OWNER, **fields):
    return dict(event_type=kind, block_number=block, transaction_index=0, log_index=log,
                owner=owner, tick_lower=-10, tick_upper=10, **fields)


@pytest.mark.parametrize("tick,expected", [
    (-11, UINT256 - 10), (-10, 50), (9, 50), (10, 10),
])
def test_fee_inside_lower_inclusive_upper_exclusive(tick, expected):
    assert fee_growth_inside(tick, -10, 10, 100, 20, 30) == expected


@pytest.mark.parametrize("side", ["lower", "upper"])
def test_crossing_preserves_earned_growth_while_inactive(side):
    before = fee_growth_inside(0, -10, 10, 100 * Q128, 20 * Q128, 30 * Q128)
    if side == "upper":
        # At crossing, upper outside becomes global - old outside.
        crossed = fee_growth_inside(10, -10, 10, 120 * Q128, 20 * Q128, 90 * Q128)
        later = fee_growth_inside(20, -10, 10, 140 * Q128, 20 * Q128, 90 * Q128)
        expected = 20
    else:
        crossed = fee_growth_inside(-11, -10, 10, 110 * Q128, 90 * Q128, 30 * Q128)
        later = fee_growth_inside(-20, -10, 10, 140 * Q128, 90 * Q128, 30 * Q128)
        expected = 10
    assert crossed == later
    assert interval_claim(before, later, 1)["fees_raw"] == expected
    assert interval_claim(crossed, later, 100)["fees_raw"] == 0


def test_both_token_growth_and_claims_stay_separate():
    starts = [fee_growth_inside(0, -10, 10, value * Q128, Q128, 2 * Q128)
              for value in (10, 20)]
    ends = [fee_growth_inside(0, -10, 10, value * Q128, Q128, 2 * Q128)
            for value in (13, 25)]
    assert [interval_claim(start, end, 7)["fees_raw"]
            for start, end in zip(starts, ends)] == [21, 35]


def test_uint256_growth_and_delta_wrap_before_multiplication():
    assert fee_growth_inside(0, -10, 10, 2, UINT256 - 3, 1) == 4
    result = interval_claim(UINT256 - Q128, Q128, 3)
    assert result == {"growth_delta_x128": 2 * Q128, "fees_raw": 6}


def test_q128_rounding_is_once_per_position_update():
    half = Q128 // 2
    separately_rounded = (interval_claim(0, half, 1)["fees_raw"]
                          + interval_claim(half, Q128, 1)["fees_raw"])
    assert separately_rounded == 0
    assert interval_claim(0, Q128, 1)["fees_raw"] == 1
    # The interval claim also differs from a pending-claim difference when
    # a fractional remainder predates the chosen starting snapshot.
    assert pending_claim(Q128, 0, 1) - pending_claim(half, 0, 1) == 1
    assert interval_claim(half, Q128, 1)["fees_raw"] == 0


@pytest.mark.parametrize("payload,expected", [
    (b"", "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"),
    (b"abc", "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"),
])
def test_ethereum_keccak_vectors(payload, expected):
    assert keccak256(payload).hex() == expected


def test_core_positions_abi_selector():
    assert keccak256(b"positions(bytes32)")[:4].hex() == "514ea4bf"


def test_position_key_packs_signed_int24_ticks(monkeypatch):
    seen = []

    def capture(payload):
        seen.append(payload)
        return bytes(32)

    monkeypatch.setattr(reference, "keccak256", capture)
    assert position_key(OWNER, -10, 10) == "0x" + "00" * 32
    assert seen == [bytes.fromhex("12" * 20 + "fffff6" + "00000a")]
    with pytest.raises(ValueError):
        position_key(OWNER, 10, 10)
    with pytest.raises(ValueError):
        position_key("0x1234", -10, 10)


def test_applicability_accepts_constant_position_and_continuous_boundaries():
    start, end = endpoints()
    end["ticks"][-10]["liquidity_gross"] = 250
    end["ticks"][10]["liquidity_gross"] = 250
    result = check_applicability(CASE, start, end, [event("Mint", amount=50)])
    assert result["status"] == "PASS"
    assert not result["reasons"]


@pytest.mark.parametrize("field,value", [
    ("liquidity", 101), ("liquidity", 0),
    ("fee_growth_inside0_last_x128", 8), ("fee_growth_inside1_last_x128", 12),
    ("tokens_owed0", 14), ("tokens_owed1", 18),
])
def test_applicability_rejects_changed_position_state(field, value):
    start, end = endpoints()
    end["position"][field] = value
    result = check_applicability(CASE, start, end, [])
    assert result["status"] == "BLOCKED"
    assert result["reasons"]


@pytest.mark.parametrize("kind", ["Mint", "Burn", "Collect"])
def test_position_updates_are_rejected_even_when_liquidity_unchanged(kind):
    start, end = endpoints()
    update = event(kind, owner=OWNER, amount=0, amount0=987654, amount1=123456)
    result = check_applicability(CASE, start, end, [update])
    assert result["status"] == "BLOCKED"
    assert result["reasons"]


def test_restored_endpoint_liquidity_does_not_hide_updates_or_principal():
    start, end = endpoints()
    events = [event("Burn", owner=OWNER, amount=100, amount0=9000, amount1=8000),
              event("Collect", owner=OWNER, log=1, amount0=9000, amount1=8000),
              event("Mint", owner=OWNER, log=2, amount=100, amount0=9000, amount1=8000)]
    # A Collect may return principal from Burn. It is not an interval fee input.
    result = check_applicability(CASE, start, end, events)
    assert result["status"] == "BLOCKED"
    assert result["reasons"]
    assert result["collected0_raw"] == result["burn_principal0_raw"] == "9000"
    assert result["collected1_raw"] == result["burn_principal1_raw"] == "8000"


def test_zero_liquidity_at_both_endpoints_cannot_supply_positive_fee_evidence():
    start, end = endpoints()
    start["position"]["liquidity"] = end["position"]["liquidity"] = 0
    assert check_applicability(CASE, start, end, [])["status"] == "BLOCKED"


def test_initialized_boundary_must_cover_the_positions_liquidity():
    start, end = endpoints()
    start["ticks"][-10]["liquidity_gross"] = end["ticks"][-10]["liquidity_gross"] = 99
    assert check_applicability(CASE, start, end, [])["status"] == "BLOCKED"


@pytest.mark.parametrize("endpoint", ["start", "end"])
def test_uninitialized_endpoint_is_unsuitable(endpoint):
    start, end = endpoints()
    selected = start if endpoint == "start" else end
    selected["ticks"][-10].update(initialized=False, liquidity_gross=0)
    result = check_applicability(CASE, start, end, [])
    assert result["status"] == "BLOCKED"


def test_deleted_then_reinitialized_boundary_is_not_a_continuous_snapshot():
    start, end = endpoints()
    result = check_applicability(CASE, start, end, [
        event("Mint", log=1, amount=200), event("Burn", log=0, amount=200),
    ])
    assert result["status"] == "BLOCKED"
    assert result["reasons"]


def test_boundary_gross_must_reconcile_with_intervening_events():
    start, end = endpoints()
    result = check_applicability(CASE, start, end, [event("Burn", amount=1)])
    assert result["status"] == "BLOCKED"


def test_end_of_block_interval_excludes_start_and_includes_end():
    start, end = endpoints()
    outside = [event("Collect", block=100, owner=OWNER, amount0=2, amount1=3),
               event("Collect", block=201, owner=OWNER, amount0=5, amount1=7),
               event("Collect", block=150, amount0=11, amount1=13)]
    assert check_applicability(CASE, start, end, outside)["status"] == "PASS"
    inside = outside + [event("Collect", block=200, owner=OWNER, amount0=2, amount1=3)]
    assert check_applicability(CASE, start, end, inside)["status"] == "BLOCKED"


def fee_endpoints(*, liquidity=100, growth0=0, growth1=0):
    start, end = endpoints()
    for point in (start, end):
        point.update(tick=0, fee_growth_global0_x128=0, fee_growth_global1_x128=0)
        point["position"].update(liquidity=liquidity, fee_growth_inside0_last_x128=0,
                                 fee_growth_inside1_last_x128=0)
        for boundary in point["ticks"].values():
            boundary.update(fee_growth_outside0_x128=0, fee_growth_outside1_x128=0)
    end.update(fee_growth_global0_x128=growth0, fee_growth_global1_x128=growth1)
    return start, end


def segment(token, fee, *, liquidity=200, lower=-1, upper=1):
    return dict(block_number=150, sqrt_start_x96=get_sqrt_ratio_at_tick(lower),
                sqrt_end_x96=get_sqrt_ratio_at_tick(upper), tick_start=lower,
                fee_token=token, fee_amount_lp=fee, liquidity=liquidity)


def test_compare_existing_position_uses_historical_liquidity_once_for_both_tokens():
    start, end = fee_endpoints(growth0=Q128, growth1=2 * Q128)
    result = compare_fees(CASE, start, end, [], [segment(0, 200), segment(1, 400)])
    assert result["status"] == "PASS"
    assert [token["reference_interval_claim_raw"] for token in result["tokens"]] == ["100", "200"]
    assert [token["event_protocol_claim_raw"] for token in result["tokens"]] == ["100", "200"]
    assert result["production"]["fees0_raw"] == Decimal(100)
    assert result["production"]["fees1_raw"] == Decimal(200)
    assert result["production"]["max_liquidity_share"] == Decimal("0.5")
    assert all(token["checks"]["growth_exact"] and token["checks"]["settled_claim_exact"]
               for token in result["tokens"])


def test_compare_fractional_marginal_fee_has_derived_rounding_difference():
    start, end = fee_endpoints(growth0=101 * Q128 // 200)
    result = compare_fees(CASE, start, end, [], [segment(0, 101)])
    token = result["tokens"][0]
    assert result["status"] == "PASS"
    assert token["reference_interval_claim_raw"] == token["event_protocol_claim_raw"] == "50"
    assert Decimal(token["production_marginal_raw"]) == Decimal("50.5")
    assert Decimal(token["production_minus_reference_raw"]) == Decimal("0.5")
    assert token["checks"]["rounding_bound_proved"]
    assert token["checks"]["production_precision_bound_proved"]


def test_compare_rounds_protocol_position_claim_after_summing_growth():
    start, end = fee_endpoints(liquidity=128, growth0=Q128 // 128)
    pieces = [segment(0, 1, liquidity=256), segment(0, 1, liquidity=256)]
    result = compare_fees(CASE, start, end, [], pieces)
    token = result["tokens"][0]
    assert result["status"] == "PASS"
    assert token["sum_separately_settled_segments_raw"] == "0"
    assert token["reference_interval_claim_raw"] == token["event_protocol_claim_raw"] == "1"


def test_opposing_token_discrepancies_fail_without_netting():
    start, end = fee_endpoints(liquidity=128, growth0=2 * Q128, growth1=0)
    pieces = [segment(0, 256, liquidity=256), segment(1, 256, liquidity=256)]
    result = compare_fees(CASE, start, end, [], pieces)
    assert result["status"] == "FAIL"
    differences = [int(token["protocol_claim_difference_raw"]) for token in result["tokens"]]
    assert differences == [-128, 128]
    assert sum(differences) == 0
    assert all(not token["checks"]["growth_exact"] for token in result["tokens"])
    assert all(not token["checks"]["settled_claim_exact"] for token in result["tokens"])


def test_partial_swap_segment_overlap_remains_blocked():
    start, end = fee_endpoints()
    result = compare_fees(CASE, start, end, [], [segment(0, 200, lower=-20, upper=0)])
    assert result["status"] == "BLOCKED"
    assert "PARTIAL_SEGMENT_OVERLAP_NOT_EXACTLY_VALIDATED" in result["applicability"]["reasons"]
    assert result["tokens"] == []

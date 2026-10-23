"""Replay tests using synthetic pool fixtures."""

from copy import deepcopy
from decimal import Decimal, localcontext

import pytest

from lp_pilot.replay import (
    PoolState, ReplayError, marginal_fees, ordered_events,
    replay_events, simulate_swap,
)
from lp_pilot.uniswap import (
    Q96, Q128, amount0_delta, amount1_delta, get_sqrt_ratio_at_tick,
    get_tick_at_sqrt_ratio,
)


BASE = 10**18


def snapshot(*, multiple=False, tick=0):
    ticks = [dict(tick=-10000, liquidity_gross=BASE, liquidity_net=BASE),
             dict(tick=10000, liquidity_gross=BASE, liquidity_net=-BASE)]
    active = BASE
    if multiple:
        ticks.extend([
            dict(tick=-20, liquidity_gross=BASE // 2, liquidity_net=BASE // 2),
            dict(tick=20, liquidity_gross=BASE // 2, liquidity_net=-(BASE // 2)),
            dict(tick=40, liquidity_gross=BASE // 3, liquidity_net=BASE // 3),
            dict(tick=80, liquidity_gross=BASE // 3, liquidity_net=-(BASE // 3)),
        ])
        active = sum(item["liquidity_net"] for item in ticks if item["tick"] <= tick)
    for item in ticks:
        item.update(fee_growth_outside0_x128=0, fee_growth_outside1_x128=0)
    return dict(sqrt_price_x96=get_sqrt_ratio_at_tick(tick), tick=tick, liquidity=active,
                ticks=ticks, ticks_complete=True, fee_pips=500, tick_spacing=10,
                fee_protocol0=0, fee_protocol1=0, block_number=100, block_hash="0x100",
                timestamp=1000, provenance="labelled synthetic unit fixture",
                fee_growth_global0_x128=0, fee_growth_global1_x128=0)


def event(kind, *, block=101, tx=0, log=0, timestamp=None, **fields):
    return dict(block_number=block, block_hash=f"0xb{block}",
                timestamp=1000 + block - 100 if timestamp is None else timestamp,
                transaction_hash=f"0xt{block}-{tx}", transaction_index=tx,
                log_index=log, event_type=kind, **fields)


def swap(state, target_tick, *, block=101, amount=10**30, enriched=True):
    state = PoolState.from_snapshot(state) if isinstance(state, dict) else state
    limit = get_sqrt_ratio_at_tick(target_tick)
    simulated = simulate_swap(state, amount, limit)
    end = simulated["state"]
    result = event("Swap", block=block, amount0=simulated["amount0"], amount1=simulated["amount1"],
                   sqrt_price_x96=end.sqrt_price_x96, tick=end.tick, liquidity=end.liquidity)
    if enriched:
        result.update(amount_specified=amount, sqrt_price_limit_x96=limit)
    return result, simulated


def test_snapshot_rejects_zero_initialization_and_incomplete_tick_distribution():
    original = snapshot()
    for change in [dict(liquidity=0), dict(ticks=[]), dict(ticks_complete=False),
                   dict(tick=100), dict(fee_protocol0=3)]:
        with pytest.raises(ReplayError):
            PoolState.from_snapshot({**original, **change})
    for field in ("provenance", "block_hash", "fee_protocol0", "tick_spacing"):
        broken = deepcopy(original)
        del broken[field]
        with pytest.raises(ReplayError):
            PoolState.from_snapshot(broken)


def test_snapshot_large_integer_strings_are_lossless_and_floats_rejected():
    original = snapshot()
    original["sqrt_price_x96"] = str(original["sqrt_price_x96"])
    original["liquidity"] = str(BASE)
    assert PoolState.from_snapshot(original).sqrt_price_x96 == Q96
    original["sqrt_price_x96"] = float(Q96)
    with pytest.raises(ReplayError, match="lossless"):
        PoolState.from_snapshot(original)


def test_snapshot_packed_protocol_fee():
    original = snapshot()
    del original["fee_protocol0"], original["fee_protocol1"]
    original["fee_protocol"] = 4 + (6 << 4)
    state = PoolState.from_snapshot(original)
    assert (state.fee_protocol0, state.fee_protocol1) == (4, 6)


def test_ordering_and_identical_duplicate_elimination():
    first = event("Collect", log=0)
    second = event("Collect", log=2, tx=1)
    third = event("Collect", block=102)
    ordered, duplicate_count = ordered_events([third, second, first, dict(first)])
    assert ordered == [first, second, third]
    assert duplicate_count == 1
    result = replay_events(snapshot(), [third, second, first, dict(first)])
    assert any(check["check"] == "duplicate_logs" and check["status"] == "WARN" for check in result.checks)


@pytest.mark.parametrize("case", ["conflicting_duplicate", "reorg", "log_index", "transaction_index", "block_time"])
def test_event_provenance_conflicts_are_rejected(case):
    first = event("Collect")
    second = dict(first)
    if case == "conflicting_duplicate":
        second["amount0"] = 1
    elif case == "reorg":
        second["block_hash"] = "0xother"
    elif case == "log_index":
        second.update(transaction_index=1, transaction_hash="0xother")
    elif case == "transaction_index":
        second.update(log_index=1, transaction_hash="0xother")
    elif case == "block_time":
        second.update(log_index=1, timestamp=1002)
    with pytest.raises(ReplayError):
        ordered_events([first, second])


def test_snapshot_must_precede_entire_first_block():
    with pytest.raises(ReplayError, match="precede"):
        replay_events(snapshot(), [event("Collect", block=100)])


def test_mint_burn_updates_active_and_inactive_liquidity_and_clears_ticks():
    events = [
        event("Mint", amount=10000, tick_lower=-100, tick_upper=100),
        event("Mint", log=1, amount=20000, tick_lower=200, tick_upper=300),
        event("Burn", log=2, amount=10000, tick_lower=-100, tick_upper=100),
    ]
    result = replay_events(snapshot(), reversed(events))
    assert result.states[1]["liquidity"] == BASE + 10000
    assert result.states[2]["liquidity"] == BASE + 10000
    assert result.state.liquidity == BASE
    assert -100 not in result.state.ticks and 100 not in result.state.ticks
    assert result.state.ticks[200].liquidity_net == 20000
    assert result.state.ticks[300].liquidity_net == -20000


def test_burn_zero_is_a_poke_and_burn_excess_fails():
    result = replay_events(snapshot(), [event("Burn", amount=0, tick_lower=-10000, tick_upper=10000)])
    assert result.state.liquidity == BASE
    with pytest.raises(ReplayError):
        replay_events(snapshot(), [event("Burn", amount=BASE + 1, tick_lower=-10000, tick_upper=10000)])


@pytest.mark.parametrize("target", [50, -50])
@pytest.mark.parametrize("enriched", [False, True])
def test_single_path_swap_both_directions(target, enriched):
    start = snapshot(tick=10)
    recorded, expected = swap(start, target, enriched=enriched)
    result = replay_events(start, [recorded])
    assert result.state.sqrt_price_x96 == expected["state"].sqrt_price_x96
    assert result.state.tick == expected["state"].tick
    assert result.state.liquidity == BASE
    assert sum(piece["fee_amount_total"] for piece in result.segments) == sum(piece["fee_amount_total"] for piece in expected["segments"])
    assert any(check["check"] == "independent_historical_checkpoints" and check["status"] == "BLOCKED" for check in result.checks)
    assert all(piece["timestamp"] == recorded["timestamp"] for piece in result.segments)


@pytest.mark.parametrize("target", [100, -100])
@pytest.mark.parametrize("enriched", [False, True])
def test_multiple_initialized_tick_crossings(target, enriched):
    start = snapshot(multiple=True)
    recorded, expected = swap(start, target, enriched=enriched)
    result = replay_events(start, [recorded])
    assert result.state.liquidity == BASE
    assert result.state.tick == target
    assert result.state.fee_growth_global0_x128 == expected["state"].fee_growth_global0_x128
    assert result.state.fee_growth_global1_x128 == expected["state"].fee_growth_global1_x128
    if target > 0:
        assert [piece["liquidity"] for piece in result.segments] == [BASE + BASE // 2, BASE, BASE + BASE // 3, BASE]
    assert result.state.ticks[20].fee_growth_outside1_x128 == expected["state"].ticks[20].fee_growth_outside1_x128


def test_uninitialized_bitmap_boundaries_are_real_fee_rounding_steps():
    start = snapshot(tick=100)
    recorded, _ = swap(start, 5200)
    result = replay_events(start, [recorded])
    assert len(result.segments) == 3
    assert [get_tick_at_sqrt_ratio(piece["sqrt_end_x96"]) for piece in result.segments] == [2550, 5110, 5200]


def test_downward_exact_boundary_tick_is_one_less_than_math_tick():
    start = snapshot(multiple=True)
    recorded, _ = swap(start, -20)
    result = replay_events(start, [recorded])
    assert result.state.sqrt_price_x96 == get_sqrt_ratio_at_tick(-20)
    assert result.state.tick == -21
    assert result.state.liquidity == BASE


def test_event_only_impossible_fee_residual_is_rejected():
    start = snapshot(tick=10)
    recorded, _ = swap(start, 20, enriched=False)
    recorded["amount1"] = 10**30
    with pytest.raises(ReplayError, match="final fee"):
        replay_events(start, [recorded])


def test_swap_integer_end_state_mismatch_is_never_tolerated():
    start = snapshot()
    recorded, _ = swap(start, 50)
    recorded["liquidity"] += 1
    with pytest.raises(ReplayError, match="liquidity"):
        replay_events(start, [recorded])


@pytest.mark.parametrize("amount", [10**12, -(10**12)])
def test_event_only_partial_exact_input_and_exact_output(amount):
    start = snapshot(tick=10)
    recorded, expected = swap(start, 100, amount=amount, enriched=False)
    result = replay_events(start, [recorded])
    assert result.state.sqrt_price_x96 == expected["state"].sqrt_price_x96
    assert sum(piece["fee_amount_total"] for piece in result.segments) == sum(piece["fee_amount_total"] for piece in expected["segments"])
    assert any(check["status"] == "WARN" and check["check"] == "swap_event_consistency" for check in result.checks)


def test_event_only_exact_output_cap_one_raw_unit_is_disclosed():
    # Numeric input from official SwapMath.spec.ts's one-unit output cap test.
    original = snapshot()
    price = 417332158212080721273783715441582
    tick = get_tick_at_sqrt_ratio(price)
    liquidity = 159344665391607089467575320103
    original.update(sqrt_price_x96=price, tick=tick, liquidity=liquidity, tick_spacing=1, fee_pips=1,
                    ticks=[dict(tick=tick - 100, liquidity_gross=liquidity, liquidity_net=liquidity),
                           dict(tick=tick + 100, liquidity_gross=liquidity, liquidity_net=-liquidity)])
    recorded = event("Swap", amount0=2, amount1=-1, sqrt_price_x96=price - 1,
                     tick=get_tick_at_sqrt_ratio(price - 1), liquidity=liquidity)
    result = replay_events(original, [recorded])
    assert any(check["check"] == "endpoint_output_rounding" and "1 raw unit" in check["detail"] for check in result.checks)


def test_tiny_fee_only_swap_after_zero_cost_tick_crossing():
    original = snapshot(multiple=True, tick=20)
    # At tick 20 the narrow position is already outside. Move the supplied
    # state to the upper-side representation before a downward crossing.
    original["tick"] = 20
    original["liquidity"] = BASE
    recorded, expected = swap(original, 10, amount=1, enriched=False)
    result = replay_events(original, [recorded])
    assert result.state.sqrt_price_x96 == get_sqrt_ratio_at_tick(20)
    assert result.state.tick == 19
    assert result.state.liquidity == BASE + BASE // 2
    assert result.segments[-1]["fee_amount_lp"] == 1
    assert result.segments[-1]["liquidity"] == BASE + BASE // 2
    assert result.state.fee_growth_global0_x128 == expected["state"].fee_growth_global0_x128


def test_full_partial_and_inactive_range_fee_allocation():
    start = snapshot(tick=10)
    recorded, _ = swap(start, 90)
    result = replay_events(start, [recorded])
    hypothetical = BASE // 10000
    full = marginal_fees(result.segments, 0, 100, hypothetical)
    partial = marginal_fees(result.segments, 30, 60, hypothetical)
    inactive = marginal_fees(result.segments, 100, 200, hypothetical)
    assert full["fees0_raw"] == 0 and full["fees1_raw"] > 0
    assert 0 < partial["fees1_raw"] < full["fees1_raw"]
    assert inactive["fees0_raw"] == inactive["fees1_raw"] == 0
    assert full["max_liquidity_share"] == Decimal("0.0001")
    piece = result.segments[0]
    with localcontext() as context:
        context.prec = 90
        expected_fraction = Decimal(amount1_delta(get_sqrt_ratio_at_tick(30), get_sqrt_ratio_at_tick(60), BASE, True)) / piece["amount_in"]
        assert partial["fees1_raw"] == full["fees1_raw"] * expected_fraction
    assert full["status"] == "WARN" and "marginal_fee_approximation" in full["flags"]


def test_historical_liquidity_changes_apply_segment_by_segment():
    original = snapshot(multiple=True)
    recorded, _ = swap(original, 100)
    segments = replay_events(original, [recorded]).segments
    result = marginal_fees(segments, -100, 200, BASE // 10000)
    with localcontext() as context:
        context.prec = 90
        expected = sum(Decimal(piece["fee_amount_lp"]) * Decimal(BASE // 10000) / piece["liquidity"] for piece in segments)
    assert result["fees1_raw"] == expected


def test_protocol_fee_change_precedes_flash_and_swaps():
    original = snapshot(tick=10)
    protocol = event("SetFeeProtocol", fee_protocol0_old=0, fee_protocol1_old=0,
                     fee_protocol0_new=4, fee_protocol1_new=5)
    flash = event("Flash", block=102, amount0=100000, amount1=200000, paid0=101, paid1=203)
    result = replay_events(original, [protocol, flash])
    assert [piece["protocol_fee"] for piece in result.segments] == [25, 40]
    assert [piece["fee_amount_lp"] for piece in result.segments] == [76, 163]
    assert result.state.fee_growth_global0_x128 == 76 * Q128 // BASE
    assert result.state.fee_growth_global1_x128 == 163 * Q128 // BASE
    assigned = marginal_fees(result.segments, 0, 100, BASE // 10000)
    assert assigned["fees0_raw"] == Decimal("0.0076")
    assert assigned["fees1_raw"] == Decimal("0.0163")


def test_collect_and_burn_principal_are_never_earned_fees():
    events = [event("Burn", amount=BASE // 2, tick_lower=-10000, tick_upper=10000,
                    amount0=10**24, amount1=10**24),
              event("Collect", log=1, amount0=10**30, amount1=10**30)]
    result = replay_events(snapshot(), events)
    assert result.segments == []
    assert result.state.fee_growth_global0_x128 == result.state.fee_growth_global1_x128 == 0
    assert result.state.liquidity == BASE // 2


def test_fee_relevant_missing_and_unsupported_events_are_blocked():
    with pytest.raises(ReplayError, match="paid0/paid1"):
        replay_events(snapshot(), [event("Flash", paid0=3)])
    with pytest.raises(ReplayError, match="unsupported"):
        replay_events(snapshot(), [event("MysteryFee")])
    with pytest.raises(ReplayError, match="old value"):
        replay_events(snapshot(), [event("SetFeeProtocol", fee_protocol0_old=4,
                                        fee_protocol0_new=4, fee_protocol1_new=4)])


@pytest.mark.parametrize("kind", ["Mint", "Burn", "Swap", "Flash", "SetFeeProtocol"])
def test_incomplete_event_fields_raise_reportable_replay_error(kind):
    with pytest.raises(ReplayError, match="required"):
        replay_events(snapshot(), [event(kind)])


@pytest.mark.parametrize("ticks", [None, [dict(tick=10)], {10: "not a tick record"}])
def test_malformed_snapshot_tick_records_raise_reportable_error(ticks):
    with pytest.raises(ReplayError, match="tick"):
        PoolState.from_snapshot({**snapshot(), "ticks": ticks})


def test_missing_fee_evidence_does_not_become_zero():
    missing = marginal_fees([dict(fee_amount_lp=None)], -100, 100, 100)
    assert missing["status"] == "BLOCKED"
    assert missing["fees0_raw"] is None and missing["fees1_raw"] is None
    empty = marginal_fees([], -100, 100, 100)
    assert empty["status"] == "WARN"
    assert "conditional_on_complete_event_coverage" in empty["flags"]


def test_replay_is_repeatable_and_does_not_mutate_inputs():
    original = snapshot(multiple=True)
    recorded, _ = swap(original, 100)
    saved_snapshot, saved_event = deepcopy(original), deepcopy(recorded)
    first = replay_events(original, [recorded])
    second = replay_events(original, [recorded])
    assert first.states == second.states
    assert first.segments == second.segments
    assert original == saved_snapshot and recorded == saved_event

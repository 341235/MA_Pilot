# SPDX-License-Identifier: GPL-2.0-or-later
# Reference work: Uniswap v3-core, Copyright (c) 2021 Uniswap Labs.
"""Replay pool events using the v3-core v1.0.0 swap loop and bitmap rules.

Marginal fee attribution uses the small-LP approximation described below.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from typing import Any, Iterable

from .uniswap import (
    FEE_DENOMINATOR, MAX_SQRT_RATIO, MAX_TICK, MAX_UINT128, MAX_UINT256,
    MIN_SQRT_RATIO, MIN_TICK, Q128, amount0_delta, amount1_delta,
    compute_swap_step, div_rounding_up, get_sqrt_ratio_at_tick, get_tick_at_sqrt_ratio,
)


class ReplayError(ValueError):
    """Insufficient evidence or an invalid protocol transition."""


def _int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ReplayError(f"{label} must be a lossless integer or decimal string")
    try:
        return int(value)
    except ValueError as error:
        raise ReplayError(f"invalid integer for {label}") from error


def _protocol(value: Any) -> int:
    result = _int(value, "protocol fee denominator")
    if result != 0 and not 4 <= result <= 10:
        raise ReplayError("protocol fee denominator must be zero or 4..10")
    return result


@dataclass
class TickState:
    liquidity_gross: int
    liquidity_net: int
    fee_growth_outside0_x128: int | None = None
    fee_growth_outside1_x128: int | None = None


@dataclass
class PoolState:
    sqrt_price_x96: int
    tick: int
    liquidity: int
    ticks: dict[int, TickState]
    fee_pips: int
    tick_spacing: int
    fee_protocol0: int
    fee_protocol1: int
    block_number: int
    block_hash: str
    timestamp: int
    provenance: str
    ticks_complete: bool = True
    fee_growth_global0_x128: int | None = None
    fee_growth_global1_x128: int | None = None

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> "PoolState":
        required = ("sqrt_price_x96", "tick", "liquidity", "ticks", "fee_pips",
                    "tick_spacing", "block_number", "block_hash", "timestamp", "provenance")
        missing = [key for key in required if key not in snapshot]
        if missing:
            raise ReplayError(f"snapshot missing required fields: {', '.join(missing)}")
        if snapshot.get("ticks_complete") is not True:
            raise ReplayError("complete initialized tick distribution is required; warm-up is insufficient")
        if not snapshot["provenance"] or not snapshot["block_hash"]:
            raise ReplayError("snapshot provenance and block hash must be recorded")
        if "fee_protocol" in snapshot:
            packed = _int(snapshot["fee_protocol"], "fee_protocol")
            if not 0 <= packed <= 255:
                raise ReplayError("packed protocol fee outside uint8")
            protocol0, protocol1 = _protocol(packed & 15), _protocol(packed >> 4)
        elif "fee_protocol0" in snapshot and "fee_protocol1" in snapshot:
            protocol0, protocol1 = _protocol(snapshot["fee_protocol0"]), _protocol(snapshot["fee_protocol1"])
        else:
            raise ReplayError("historical protocol fee state is required")
        ticks: dict[int, TickState] = {}
        raw_ticks = snapshot["ticks"]
        if not isinstance(raw_ticks, (list, tuple, dict)):
            raise ReplayError("snapshot ticks must be a list or dictionary of initialized ticks")
        if isinstance(raw_ticks, dict) and any(not isinstance(value, dict) for value in raw_ticks.values()):
            raise ReplayError("snapshot tick dictionary values must be records")
        iterable = ({"tick": tick, **value} for tick, value in raw_ticks.items()) if isinstance(raw_ticks, dict) else raw_ticks
        for row in iterable:
            if not isinstance(row, dict) or any(name not in row for name in ("tick", "liquidity_gross", "liquidity_net")):
                raise ReplayError("snapshot tick requires tick, liquidity_gross, liquidity_net")
            tick = _int(row["tick"], "initialized tick")
            if tick in ticks:
                raise ReplayError("duplicate initialized tick")
            ticks[tick] = TickState(
                _int(row["liquidity_gross"], "liquidity_gross"),
                _int(row["liquidity_net"], "liquidity_net"),
                _optional_int(row.get("fee_growth_outside0_x128"), "fee_growth_outside0_x128"),
                _optional_int(row.get("fee_growth_outside1_x128"), "fee_growth_outside1_x128"),
            )
        state = cls(
            **{name: _int(snapshot[name], name) for name in (
                "sqrt_price_x96", "tick", "liquidity", "fee_pips", "tick_spacing", "block_number", "timestamp")},
            ticks=ticks, fee_protocol0=protocol0, fee_protocol1=protocol1,
            block_hash=str(snapshot["block_hash"]), provenance=str(snapshot["provenance"]),
            fee_growth_global0_x128=_optional_int(snapshot.get("fee_growth_global0_x128"), "fee_growth_global0_x128"),
            fee_growth_global1_x128=_optional_int(snapshot.get("fee_growth_global1_x128"), "fee_growth_global1_x128"),
        )
        state.validate()
        return state

    def validate(self) -> None:
        if not MIN_SQRT_RATIO <= self.sqrt_price_x96 < MAX_SQRT_RATIO:
            raise ReplayError("snapshot sqrt price outside pool bounds")
        mathematical_tick = get_tick_at_sqrt_ratio(self.sqrt_price_x96)
        valid_ticks = {mathematical_tick}
        if self.sqrt_price_x96 == get_sqrt_ratio_at_tick(mathematical_tick):
            valid_ticks.add(mathematical_tick - 1)
        if self.tick not in valid_ticks or not MIN_TICK <= self.tick <= MAX_TICK:
            raise ReplayError("tick inconsistent with sqrt price (including downward crossing convention)")
        if not 0 <= self.liquidity <= MAX_UINT128:
            raise ReplayError("active liquidity outside uint128")
        if not 0 < self.tick_spacing <= 16384 or not 0 <= self.fee_pips < FEE_DENOMINATOR:
            raise ReplayError("invalid tick spacing or fee")
        min_usable = -((-MIN_TICK) // self.tick_spacing) * self.tick_spacing
        max_usable = MAX_TICK // self.tick_spacing * self.tick_spacing
        max_per_tick = MAX_UINT128 // ((max_usable - min_usable) // self.tick_spacing + 1)
        for tick, data in self.ticks.items():
            if not MIN_TICK <= tick <= MAX_TICK or tick % self.tick_spacing:
                raise ReplayError("initialized tick outside bounds or spacing")
            if not 0 < data.liquidity_gross <= max_per_tick or abs(data.liquidity_net) > data.liquidity_gross:
                raise ReplayError("invalid gross/net tick liquidity")
            if not -(1 << 127) <= data.liquidity_net < 1 << 127:
                raise ReplayError("tick net liquidity outside int128")
            for outside in (data.fee_growth_outside0_x128, data.fee_growth_outside1_x128):
                if outside is not None and not 0 <= outside <= MAX_UINT256:
                    raise ReplayError("fee growth outside uint256")
        if sum(row.liquidity_net for row in self.ticks.values()) != 0:
            raise ReplayError("complete tick distribution must have zero net liquidity sum")
        if sum(row.liquidity_net for tick, row in self.ticks.items() if tick <= self.tick) != self.liquidity:
            raise ReplayError("active liquidity disagrees with complete initialized tick distribution")
        for growth in (self.fee_growth_global0_x128, self.fee_growth_global1_x128):
            if growth is not None and not 0 <= growth <= MAX_UINT256:
                raise ReplayError("global fee growth outside uint256")


def _optional_int(value: Any, label: str) -> int | None:
    return None if value is None else _int(value, label)


@dataclass
class ReplayResult:
    state: PoolState
    states: list[dict[str, Any]] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, str]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


def ordered_events(events: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """One pool log stream; reject reorg conflicts and conflicting duplicates."""
    by_key: dict[tuple[int, int, int], dict[str, Any]] = {}
    block_hashes: dict[int, str] = {}
    transaction_positions: dict[str, tuple[int, int]] = {}
    block_log_positions: dict[tuple[int, int], int] = {}
    transaction_indices: dict[tuple[int, int], str] = {}
    removed = 0
    for original in events:
        event = dict(original)
        required = ("block_number", "block_hash", "timestamp", "transaction_hash",
                    "transaction_index", "log_index", "event_type")
        if any(name not in event for name in required):
            raise ReplayError("event missing canonical block/transaction/log provenance")
        if event.get("removed"):
            raise ReplayError("removed/reorg log cannot be replayed")
        for name in ("block_number", "timestamp", "transaction_index", "log_index"):
            event[name] = _int(event[name], name)
            if event[name] < 0:
                raise ReplayError("negative event ordering or timestamp field")
        number, block_hash = event["block_number"], str(event["block_hash"])
        if number in block_hashes and block_hashes[number] != block_hash:
            raise ReplayError("conflicting block hashes; resolve canonical chain before replay")
        block_hashes[number] = block_hash
        transaction = str(event["transaction_hash"])
        position = (number, event["transaction_index"])
        if transaction in transaction_positions and transaction_positions[transaction] != position:
            raise ReplayError("transaction hash has conflicting block/transaction positions")
        transaction_positions[transaction] = position
        if position in transaction_indices and transaction_indices[position] != transaction:
            raise ReplayError("transaction index has conflicting transaction hashes")
        transaction_indices[position] = transaction
        block_log = (number, event["log_index"])
        if block_log in block_log_positions and block_log_positions[block_log] != event["transaction_index"]:
            raise ReplayError("block log index appears in multiple transactions")
        block_log_positions[block_log] = event["transaction_index"]
        key = (number, event["transaction_index"], event["log_index"])
        if key in by_key:
            if by_key[key] != event:
                raise ReplayError("conflicting duplicate event")
            removed += 1
        else:
            by_key[key] = event
    result = [by_key[key] for key in sorted(by_key)]
    previous_time = -1
    seen_block_times: dict[int, int] = {}
    for event in result:
        if event["timestamp"] < previous_time:
            raise ReplayError("block timestamps decrease in canonical event order")
        number, timestamp = event["block_number"], event["timestamp"]
        if number in seen_block_times and seen_block_times[number] != timestamp:
            raise ReplayError("events in a block have inconsistent timestamps")
        seen_block_times[number] = timestamp
        previous_time = timestamp
    return result, removed


def _next_tick(state: PoolState, zero_for_one: bool) -> tuple[int, bool]:
    """Include empty bitmap word edges; these are actual swap steps too."""
    compressed = state.tick // state.tick_spacing
    if zero_for_one:
        low = (compressed >> 8) << 8
        candidates = [tick for tick in state.ticks if low <= tick // state.tick_spacing <= compressed]
        next_tick = max(candidates) if candidates else low * state.tick_spacing
    else:
        compressed += 1
        high = ((compressed >> 8) << 8) + 255
        candidates = [tick for tick in state.ticks if compressed <= tick // state.tick_spacing <= high]
        next_tick = min(candidates) if candidates else high * state.tick_spacing
    next_tick = min(MAX_TICK, max(MIN_TICK, next_tick))
    return next_tick, next_tick in state.ticks


def _add_fees(state: PoolState, token: int, total_fee: int) -> tuple[int, int]:
    if total_fee < 0:
        raise ReplayError("negative fee inferred from event amounts")
    denominator = state.fee_protocol0 if token == 0 else state.fee_protocol1
    protocol_fee = total_fee // denominator if denominator else 0
    lp_fee = total_fee - protocol_fee
    if lp_fee and state.liquidity == 0:
        raise ReplayError("fee donation with zero active historical liquidity")
    attribute = f"fee_growth_global{token}_x128"
    growth = getattr(state, attribute)
    if growth is not None and state.liquidity:
        setattr(state, attribute, (growth + lp_fee * Q128 // state.liquidity) & MAX_UINT256)
    return lp_fee, protocol_fee


def _cross_or_update(state: PoolState, next_price: int, next_tick: int,
                     initialized: bool, zero_for_one: bool) -> None:
    if next_price == get_sqrt_ratio_at_tick(next_tick):
        if initialized:
            data = state.ticks[next_tick]
            state.liquidity += -data.liquidity_net if zero_for_one else data.liquidity_net
            if not 0 <= state.liquidity <= MAX_UINT128:
                raise ReplayError("crossing produces invalid active liquidity")
            for token in (0, 1):
                attribute = f"fee_growth_outside{token}_x128"
                outside = getattr(data, attribute)
                growth = getattr(state, f"fee_growth_global{token}_x128")
                setattr(data, attribute, (growth - outside) & MAX_UINT256 if growth is not None and outside is not None else None)
        state.tick = next_tick - 1 if zero_for_one else next_tick
    elif next_price != state.sqrt_price_x96:
        state.tick = get_tick_at_sqrt_ratio(next_price)
    state.sqrt_price_x96 = next_price


def _segment(state: PoolState, next_price: int, amount_in: int, amount_out: int,
             total_fee: int, zero_for_one: bool, exactness: str) -> dict[str, Any]:
    token = 0 if zero_for_one else 1
    lp_fee, protocol_fee = _add_fees(state, token, total_fee)
    return dict(
        sqrt_start_x96=state.sqrt_price_x96, sqrt_end_x96=next_price,
        tick_start=state.tick, liquidity=state.liquidity,
        amount_in=amount_in, amount_out=amount_out, fee_token=token,
        fee_amount_total=total_fee, fee_amount_lp=lp_fee, protocol_fee=protocol_fee,
        zero_for_one=zero_for_one, exactness=exactness, event_type="Swap",
    )


def simulate_swap(state: PoolState, amount_specified: int,
                  sqrt_price_limit_x96: int) -> dict[str, Any]:
    """Simulate a swap without mutating the input state.

    Returns state, segments, amount0 and amount1. The price limit determines
    direction; negative amount_specified means exact output. This models the
    swap loop only, excluding callbacks and pool balance checks.
    """
    state = deepcopy(state)
    specified, limit = _int(amount_specified, "amount_specified"), _int(sqrt_price_limit_x96, "sqrt_price_limit_x96")
    if not specified or not -(1 << 255) < specified < 1 << 255:
        raise ReplayError("nonzero safely negatable int256 amount_specified required")
    zero = limit < state.sqrt_price_x96
    if not MIN_SQRT_RATIO < limit < MAX_SQRT_RATIO or limit == state.sqrt_price_x96:
        raise ReplayError("invalid swap price limit")
    remaining, calculated = specified, 0
    exact_in = specified > 0
    segments: list[dict[str, Any]] = []
    for _ in range(100_000):
        if remaining == 0 or state.sqrt_price_x96 == limit:
            break
        next_tick, initialized = _next_tick(state, zero)
        boundary = get_sqrt_ratio_at_tick(next_tick)
        target = max(limit, boundary) if zero else min(limit, boundary)
        step = compute_swap_step(state.sqrt_price_x96, target, state.liquidity, remaining, state.fee_pips)
        segments.append(_segment(state, step.sqrt_price_next_x96, step.amount_in,
                                 step.amount_out, step.fee_amount, zero, "reference_exact"))
        if exact_in:
            remaining -= step.amount_in + step.fee_amount
            calculated -= step.amount_out
        else:
            remaining += step.amount_out
            calculated += step.amount_in + step.fee_amount
        _cross_or_update(state, step.sqrt_price_next_x96, next_tick, initialized, zero)
    else:
        raise ReplayError("swap exceeds bounded replay step budget")
    if zero == exact_in:
        amount0, amount1 = specified - remaining, calculated
    else:
        amount0, amount1 = calculated, specified - remaining
    return dict(state=state, segments=segments, amount0=amount0, amount1=amount1)


def _event_endpoint_swap(state: PoolState, event: dict[str, Any]) -> tuple[PoolState, list[dict[str, Any]], int]:
    """Endpoint-conditioned path; direction/call mode and final rounding limited."""
    target = _int(event["sqrt_price_x96"], "sqrt_price_x96")
    a0, a1 = _int(event["amount0"], "amount0"), _int(event["amount1"], "amount1")
    if a0 > 0 and a1 <= 0:
        zero, gross, observed_output = True, a0, -a1
    elif a1 > 0 and a0 <= 0:
        zero, gross, observed_output = False, a1, -a0
    else:
        raise ReplayError("swap requires one positive input and one nonpositive output")
    if (zero and target > state.sqrt_price_x96) or (not zero and target < state.sqrt_price_x96):
        raise ReplayError("swap signs disagree with price direction")
    if not MIN_SQRT_RATIO < target < MAX_SQRT_RATIO:
        raise ReplayError("swap endpoint outside pool bounds")
    segments: list[dict[str, Any]] = []
    remaining_gross, reconstructed_output = gross, 0
    for _ in range(100_000):
        next_tick, initialized = _next_tick(state, zero)
        boundary = get_sqrt_ratio_at_tick(next_tick)
        next_price = max(target, boundary) if zero else min(target, boundary)
        delta_in = amount0_delta if zero else amount1_delta
        delta_out = amount1_delta if zero else amount0_delta
        amount_in = delta_in(state.sqrt_price_x96, next_price, state.liquidity, True)
        amount_out = delta_out(state.sqrt_price_x96, next_price, state.liquidity, False)
        # At a downward boundary the pool first performs a zero-cost crossing.
        # A tiny input may then be entirely fees at the unchanged sqrt price,
        # earned by the liquidity active AFTER that crossing.
        zero_cost_crossing = (zero and next_price == state.sqrt_price_x96 == boundary
                              and state.tick == next_tick)
        last = next_price == target and not zero_cost_crossing
        fee = remaining_gross - amount_in if last else div_rounding_up(
            amount_in * state.fee_pips, FEE_DENOMINATOR - state.fee_pips)
        if last:
            standard_fee = div_rounding_up(amount_in * state.fee_pips,
                                          FEE_DENOMINATOR - state.fee_pips)
            if fee != standard_fee:
                # A partial exact-input step can spend a rounding remainder as
                # fee. Check it through SwapMath; arbitrary event residuals do
                # not establish fee income merely by subtraction.
                candidate = compute_swap_step(state.sqrt_price_x96, boundary,
                                               state.liquidity, remaining_gross,
                                               state.fee_pips)
                if (candidate.sqrt_price_next_x96 != next_price
                        or candidate.amount_in != amount_in or candidate.fee_amount != fee):
                    raise ReplayError("event final fee cannot arise from reference SwapMath rounding")
        segment = _segment(state, next_price, amount_in, amount_out, fee, zero, "event_endpoint_approximation")
        segments.append(segment)
        remaining_gross -= amount_in + fee
        reconstructed_output += amount_out
        _cross_or_update(state, next_price, next_tick, initialized, zero)
        if last:
            break
    else:
        raise ReplayError("event path exceeds bounded replay step budget")
    difference = reconstructed_output - observed_output
    # Exact-output final amount can be capped by one raw unit after sqrt rounding.
    if difference not in (0, 1):
        raise ReplayError(f"event output disagrees with integer path by {difference} raw units")
    return state, segments, difference


def _update_liquidity(state: PoolState, event: dict[str, Any], is_mint: bool) -> None:
    lower, upper, amount = (_int(event[name], name) for name in ("tick_lower", "tick_upper", "amount"))
    if lower >= upper or lower < MIN_TICK or upper > MAX_TICK or lower % state.tick_spacing or upper % state.tick_spacing:
        raise ReplayError("invalid Mint/Burn tick bounds")
    if amount < 0 or amount > MAX_UINT128 or (is_mint and amount == 0):
        raise ReplayError("invalid Mint/Burn liquidity amount")
    change = amount if is_mint else -amount
    for tick, sign in ((lower, 1), (upper, -1)):
        if tick not in state.ticks:
            if change < 0:
                raise ReplayError("Burn references uninitialized tick")
            state.ticks[tick] = TickState(
                0, 0,
                state.fee_growth_global0_x128 if tick <= state.tick else 0,
                state.fee_growth_global1_x128 if tick <= state.tick else 0,
            )
        data = state.ticks[tick]
        data.liquidity_gross += change
        data.liquidity_net += sign * change
        if data.liquidity_gross < 0:
            raise ReplayError("Burn exceeds initialized tick liquidity")
        if data.liquidity_gross == 0:
            if data.liquidity_net != 0:
                raise ReplayError("zero gross tick retains nonzero net liquidity")
            del state.ticks[tick]
    if lower <= state.tick < upper:
        state.liquidity += change
    state.validate()


def _state_row(state: PoolState, event: dict[str, Any] | None = None) -> dict[str, Any]:
    row = dict(block_number=state.block_number, block_hash=state.block_hash,
               timestamp=state.timestamp, available_at=state.timestamp,
               sqrt_price_x96=state.sqrt_price_x96, tick=state.tick, liquidity=state.liquidity,
               fee_growth_global0_x128=state.fee_growth_global0_x128,
               fee_growth_global1_x128=state.fee_growth_global1_x128,
               fee_protocol0=state.fee_protocol0, fee_protocol1=state.fee_protocol1,
               event_type="Snapshot")
    if event is not None:
        row.update({name: event[name] for name in (
            "event_type", "transaction_hash", "transaction_index", "log_index")})
    else:
        row.update(transaction_hash=None, transaction_index=-1, log_index=-1)
    return row


def replay_events(snapshot: dict[str, Any] | PoolState,
                  events: Iterable[dict[str, Any]]) -> ReplayResult:
    state = deepcopy(snapshot) if isinstance(snapshot, PoolState) else PoolState.from_snapshot(snapshot)
    state.validate()
    ordered, duplicates = ordered_events(events)
    if ordered and (ordered[0]["block_number"] <= state.block_number or ordered[0]["timestamp"] < state.timestamp):
        raise ReplayError("snapshot must precede the first replayed block")
    result = ReplayResult(state=state, events=ordered, states=[_state_row(state)])
    result.checks.append(dict(check="initial_tick_distribution", status="PASS",
                              detail="Complete supplied tick map has consistent net and active liquidity; provenance requires independent verification."))
    if duplicates:
        result.checks.append(dict(check="duplicate_logs", status="WARN", detail=f"Removed {duplicates} identical duplicate pool logs."))
    endpoint_count = 0
    for event in ordered:
        kind = event["event_type"]
        required_fields = {
            "Mint": ("tick_lower", "tick_upper", "amount"),
            "Burn": ("tick_lower", "tick_upper", "amount"),
            "Swap": ("amount0", "amount1", "sqrt_price_x96", "tick", "liquidity"),
            "Flash": ("paid0", "paid1"),
            "SetFeeProtocol": ("fee_protocol0_new", "fee_protocol1_new"),
        }.get(kind, ())
        missing_fields = [name for name in required_fields if name not in event]
        if missing_fields:
            if kind == "Flash":
                raise ReplayError("Flash paid0/paid1 required; fee reconstruction blocked")
            raise ReplayError(f"{kind} event missing required fields: {', '.join(missing_fields)}")
        segments: list[dict[str, Any]] = []
        if kind in ("Mint", "Burn"):
            _update_liquidity(state, event, kind == "Mint")
        elif kind == "Swap":
            if "amount_specified" in event and "sqrt_price_limit_x96" in event:
                simulation = simulate_swap(state, event["amount_specified"], event["sqrt_price_limit_x96"])
                for token in (0, 1):
                    if simulation[f"amount{token}"] != _int(event[f"amount{token}"], f"amount{token}"):
                        raise ReplayError("enriched swap input/output differs from recorded event")
                state, segments = simulation["state"], simulation["segments"]
            else:
                state, segments, output_difference = _event_endpoint_swap(state, event)
                endpoint_count += 1
                if output_difference:
                    result.checks.append(dict(check="endpoint_output_rounding", status="WARN",
                                              detail="Endpoint delta exceeds emitted output by 1 raw unit, consistent with exact-output capping; call mode is unobserved."))
            for name in ("sqrt_price_x96", "tick", "liquidity"):
                if getattr(state, name) != _int(event[name], name):
                    raise ReplayError(f"reconstructed swap {name} differs from event; event consistency failed")
            state.validate()
        elif kind == "Flash":
            if state.liquidity == 0:
                raise ReplayError("Flash requires positive active pool liquidity")
            for token in (0, 1):
                field_name = f"paid{token}"
                if field_name not in event:
                    raise ReplayError("Flash paid0/paid1 required; fee reconstruction blocked")
                paid = _int(event[field_name], field_name)
                lp, protocol = _add_fees(state, token, paid)
                segments.append(dict(sqrt_start_x96=state.sqrt_price_x96,
                                     sqrt_end_x96=state.sqrt_price_x96,
                                     tick_start=state.tick, liquidity=state.liquidity,
                                     amount_in=0, amount_out=0, fee_token=token,
                                     fee_amount_total=paid, fee_amount_lp=lp, protocol_fee=protocol,
                                     zero_for_one=token == 0, exactness="flash_event_accounting", event_type="Flash"))
        elif kind == "SetFeeProtocol":
            for token in (0, 1):
                new_key = f"fee_protocol{token}_new"
                old_key = f"fee_protocol{token}_old"
                if new_key not in event:
                    raise ReplayError("SetFeeProtocol requires both new denominators")
                if old_key in event and _protocol(event[old_key]) != getattr(state, f"fee_protocol{token}"):
                    raise ReplayError("protocol fee old value disagrees with replay")
                setattr(state, f"fee_protocol{token}", _protocol(event[new_key]))
        elif kind in ("Collect", "CollectProtocol", "IncreaseObservationCardinalityNext"):
            # Collect transfers accrued owed tokens. Its amount0/amount1 can mix
            # burned principal and earned fees, so it is never fee income here.
            pass
        else:
            raise ReplayError(f"unsupported pool event {kind}; reconstruction blocked")
        state.block_number, state.block_hash, state.timestamp = event["block_number"], event["block_hash"], event["timestamp"]
        metadata = {name: event[name] for name in (
            "block_number", "block_hash", "timestamp", "transaction_hash", "transaction_index", "log_index")}
        for index, segment in enumerate(segments):
            segment.update(metadata, segment_index=index, available_at=event["timestamp"])
        result.segments.extend(segments)
        result.states.append(_state_row(state, event))
    result.state = state
    result.checks.append(dict(check="swap_event_consistency", status="WARN" if endpoint_count else "PASS",
                              detail=f"{endpoint_count} endpoint-conditioned swaps. Event comparisons are internal consistency, never independent checkpoints."))
    result.checks.append(dict(check="independent_historical_checkpoints", status="BLOCKED",
                              detail="Supply independently sourced historical state and fee-growth checkpoints; event endpoints do not qualify."))
    return result


def marginal_fees(segments: Iterable[dict[str, Any]], lower_tick: int,
                  upper_tick: int, liquidity: int) -> dict[str, Any]:
    """Attribute historical LP fees to a small hypothetical LP, without impact.

    For a partial segment, interpolate fees by its rounded input-token delta.
    Fee share is L_hyp/L_historical, not L_hyp/(L_hyp+L_historical). Fractional
    raw token amounts are preserved as Decimal; this is a marginal approximation
    and does not claim actual feeGrowthInside or mint/burn settlement rounding.
    """
    if lower_tick >= upper_tick or liquidity < 0 or liquidity > MAX_UINT128:
        raise ReplayError("invalid hypothetical position")
    lower, upper = get_sqrt_ratio_at_tick(lower_tick), get_sqrt_ratio_at_tick(upper_tick)
    fees = [Decimal(0), Decimal(0)]
    max_share = Decimal(0)
    flags = {"marginal_fee_approximation", "conditional_on_complete_event_coverage"}
    with localcontext() as context:
        context.prec = 90
        for segment in segments:
            if segment.get("fee_amount_lp") is None:
                return dict(fees0_raw=None, fees1_raw=None, max_liquidity_share=None,
                            status="BLOCKED", flags=sorted(flags | {"missing_fee_evidence"}))
            start, end = _int(segment["sqrt_start_x96"], "sqrt_start"), _int(segment["sqrt_end_x96"], "sqrt_end")
            historical = _int(segment["liquidity"], "historical liquidity")
            if start == end:
                active = lower_tick <= _int(segment.get("tick_start", get_tick_at_sqrt_ratio(start)), "tick_start") < upper_tick
                overlap_fraction = Decimal(1) if active else Decimal(0)
            else:
                lo, hi = sorted((start, end))
                overlap_low, overlap_high = max(lo, lower), min(hi, upper)
                active = overlap_high > overlap_low
                if active and historical:
                    delta = amount0_delta if int(segment["fee_token"]) == 0 else amount1_delta
                    full = delta(lo, hi, historical, True)
                    part = delta(overlap_low, overlap_high, historical, True)
                    overlap_fraction = min(Decimal(1), Decimal(part) / full) if full else Decimal(1)
                else:
                    overlap_fraction = Decimal(0)
            if not active or liquidity == 0:
                continue
            if historical == 0:
                return dict(fees0_raw=None, fees1_raw=None, max_liquidity_share=None,
                            status="BLOCKED", flags=sorted(flags | {"zero_historical_liquidity"}))
            share = Decimal(liquidity) / historical
            max_share = max(max_share, share)
            fee_token = _int(segment["fee_token"], "fee_token")
            if fee_token not in (0, 1):
                raise ReplayError("fee token must be zero or one")
            fee = _int(segment["fee_amount_lp"], "fee_amount_lp")
            if fee < 0:
                raise ReplayError("negative LP fee")
            fees[fee_token] += Decimal(fee) * overlap_fraction * share
            if segment.get("exactness") == "event_endpoint_approximation":
                flags.add("event_only_fee_path")
        return dict(fees0_raw=+fees[0], fees1_raw=+fees[1], max_liquidity_share=+max_share,
                    status="WARN", flags=sorted(flags))

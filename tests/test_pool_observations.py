"""Block coverage, rather than trading activity, establishes pool observation age."""

from copy import deepcopy

import pytest

from lp_pilot.pool_observations import confirm_pool_observations
from lp_pilot.temporal import apply_availability, backward_asof


def snapshot():
    return {
        "block_number": 100, "block_hash": "0xb100", "timestamp": 1000,
        "sqrt_price_x96": 2**96, "tick": 0, "liquidity": 1000,
        "fee_growth_global0_x128": 0, "fee_growth_global1_x128": 0,
        "fee_protocol0": 0, "fee_protocol1": 0,
        "state_changed_at": 1000, "state_changed_block_number": 100,
    }


def header(number, timestamp):
    return {"block_number": number, "block_hash": f"0xb{number}", "timestamp": timestamp}


def event(kind, number, timestamp, log=0, **fields):
    return {
        **header(number, timestamp), "event_type": kind,
        "transaction_index": 0, "log_index": log,
        "transaction_hash": f"0xt{number}", **fields,
    }


def manifest(first, stop, events, *, chunks=None):
    return {
        "coverage_complete": True, "block_start": first,
        "block_end_exclusive": stop, "source_row_count": len(events),
        "chunks": chunks if chunks is not None else [{
            "from_block": first, "to_block": stop - 1, "row_count": len(events),
        }],
    }


def state_rows(events, *, updates=None):
    current = snapshot()
    rows = [dict(current)]
    for index, item in enumerate(events):
        current = {**current, **item, **((updates or {}).get(index, {}))}
        rows.append(dict(current))
    return rows


def confirm(events, headers, *, states=None, proof=None):
    proof = proof or manifest(101, max(row["block_number"] for row in headers) + 1, events)
    return confirm_pool_observations(
        snapshot(), events, states if states is not None else state_rows(events), proof, headers,
    )[0]


def test_long_quiet_pool_is_fresh_only_at_proved_header_times():
    headers = [header(number, 1000 + (number - 100) * 12) for number in range(101, 122)]
    observations = confirm([], headers)
    latest = observations[-1]
    assert latest["timestamp"] == 1252
    assert latest["state_changed_at"] == 1000
    assert latest["state_changed_block_number"] == 100
    assert latest["observed_block_number"] == 121
    assert latest["observed_block_hash"] == "0xb121"
    assert len(observations) == len(headers)
    assert backward_asof(apply_availability(observations, 12), 1264, 120)["status"] == "PASS"
    assert snapshot()["timestamp"] == 1000


def test_snapshot_time_does_not_invent_unknown_historical_change_time():
    initial = snapshot()
    del initial["state_changed_at"], initial["state_changed_block_number"]
    observations, _ = confirm_pool_observations(
        initial, [], [dict(initial)], manifest(101, 103, []),
        [header(101, 1012), header(102, 1024)],
    )
    assert observations[-1]["state_changed_at"] is None
    assert observations[-1]["state_changed_block_number"] is None
    assert observations[-1]["state_anchor_at"] == 1000
    assert observations[-1]["timestamp"] == 1024


def test_missing_heartbeat_header_is_not_fabricated_from_coverage():
    observations = confirm([], [header(101, 1012), header(120, 1240)])
    assert [row["observed_block_number"] for row in observations] == [101, 120]
    old = backward_asof(apply_availability(observations, 12), 1239, 120)
    assert old["status"] == "BLOCKED"
    assert old["age_seconds"] == 227


def test_covered_quiet_block_can_precede_first_event_after_snapshot():
    events = [event("Swap", 102, 1024)]
    states = state_rows(events, updates={0: {"tick": 10}})
    observations = confirm(events, [header(101, 1012), header(102, 1024), header(103, 1036)],
                           states=states)
    assert observations[0]["state_changed_at"] == 1000
    assert observations[0]["tick"] == 0
    assert observations[1]["state_changed_at"] == 1024
    assert observations[2]["tick"] == 10


def test_gap_in_pool_log_windows_cannot_confirm_later_header():
    proof = manifest(101, 105, [], chunks=[
        {"from_block": 101, "to_block": 101, "row_count": 0},
        {"from_block": 103, "to_block": 104, "row_count": 0},
    ])
    with pytest.raises(ValueError):
        confirm([], [header(101, 1012), header(104, 1048)], proof=proof)


@pytest.mark.parametrize("change", [
    {"coverage_complete": False}, {"source_row_count": 1},
    {"block_end_exclusive": 105},
])
def test_incomplete_or_contradictory_coverage_is_rejected(change):
    proof = {**manifest(101, 103, []), **change}
    with pytest.raises(ValueError):
        confirm([], [header(101, 1012), header(102, 1024)], proof=proof)


def test_event_without_header_cannot_be_skipped_in_confirmed_prefix():
    events = [event("Collect", 102, 1024)]
    with pytest.raises(ValueError):
        confirm(events, [header(101, 1012), header(103, 1036)])


@pytest.mark.parametrize("change", [{"block_hash": "0xconflict"}, {"timestamp": 1013}])
def test_event_header_disagreement_is_rejected(change):
    events = [{**event("Collect", 101, 1012), **change}]
    with pytest.raises(ValueError):
        confirm(events, [header(101, 1012)])


def test_only_last_replayed_event_can_confirm_a_multi_event_block():
    events = [event("Swap", 101, 1012), event("Swap", 101, 1012, log=1),
              event("Collect", 101, 1012, log=2)]
    states = state_rows(events, updates={0: {"tick": 10}, 1: {"tick": 20}})
    observations = confirm(events, [header(101, 1012), header(102, 1024)], states=states)
    assert len(observations) == 2
    assert observations[0]["tick"] == observations[1]["tick"] == 20
    assert observations[0]["state_changed_at"] == 1012
    assert observations[1]["state_changed_at"] == 1012
    assert observations[1]["timestamp"] == 1024
    assert states[-1]["timestamp"] == 1012


def test_missing_replayed_event_state_prevents_end_of_block_confirmation():
    events = [event("Collect", 101, 1012), event("Collect", 101, 1012, log=1)]
    with pytest.raises(ValueError):
        confirm(events, [header(101, 1012)], states=state_rows(events)[:-1])


def test_duplicate_replayed_event_state_is_not_silently_selected():
    events = [event("Collect", 101, 1012)]
    states = state_rows(events)
    with pytest.raises(ValueError):
        confirm(events, [header(101, 1012)], states=states + [dict(states[-1])])


def test_log_coverage_must_bridge_snapshot_to_first_observation():
    proof = manifest(102, 104, [])
    with pytest.raises(ValueError):
        confirm([], [header(102, 1024), header(103, 1036)], proof=proof)


def test_collect_updates_last_event_but_not_last_relevant_state_change():
    events = [event("Swap", 101, 1012), event("Collect", 102, 1024)]
    states = state_rows(events, updates={0: {"tick": 10}})
    latest = confirm(events, [header(101, 1012), header(102, 1024)], states=states)[-1]
    assert latest["last_event_at"] == 1024
    assert latest["state_changed_at"] == 1012
    assert latest["state_changed_block_number"] == 101


@pytest.mark.parametrize("kind", ["Mint", "Burn"])
def test_off_range_liquidity_event_still_changes_tick_state(kind):
    events = [event(kind, 101, 1012, amount=1, tick_lower=200, tick_upper=300)]
    latest = confirm(events, [header(101, 1012)])[-1]
    assert latest["liquidity"] == snapshot()["liquidity"]
    assert latest["tick"] == snapshot()["tick"]
    assert latest["state_changed_at"] == 1012


def test_unknown_pool_event_prevents_coverage_confirmation():
    events = [event("UnrecognizedEvent", 101, 1012)]
    with pytest.raises(ValueError):
        confirm(events, [header(101, 1012)])


def test_end_of_block_explicit_availability_includes_every_event():
    events = [event("Collect", 101, 1012, available_at=1100),
              event("Collect", 101, 1012, log=1, available_at=1020)]
    observations = confirm(events, [header(101, 1012), header(102, 1024)])
    assert observations[0]["available_at"] == observations[1]["available_at"] == 1100
    assert backward_asof(observations, 1099, 120)["row"] is None
    assert backward_asof(observations, 1100, 120)["row"]["timestamp"] == 1024


def test_delayed_availability_and_exact_observation_age_boundary():
    observations = confirm([], [header(101, 1012), header(102, 1024)])
    delayed = apply_availability(observations, 12)
    assert backward_asof(delayed, 1023.999, 120)["row"] is None
    assert backward_asof(delayed, 1024, 120)["row"]["observed_block_number"] == 101
    assert backward_asof(delayed, 1035.999, 120)["row"]["observed_block_number"] == 101
    assert backward_asof(delayed, 1036, 120)["row"]["observed_block_number"] == 102
    assert backward_asof(delayed, 1144, 120)["age_seconds"] == 120
    assert backward_asof(delayed, 1144, 120)["status"] == "PASS"
    assert backward_asof(delayed, 1144.001, 120)["status"] == "BLOCKED"


def test_larger_availability_delay_does_not_make_an_old_header_fresh():
    observations = confirm([], [header(101, 1012)])
    result = backward_asof(apply_availability(observations, 200), 1212, 120)
    assert result["status"] == "BLOCKED"
    assert result["age_seconds"] == 200
    assert result["candidate"]["timestamp"] == 1012


def test_future_available_events_do_not_change_past_observations():
    events = [event("Swap", 101, 1012), event("Swap", 102, 1024, available_at=1100)]
    states = state_rows(events, updates={0: {"tick": 10}, 1: {"tick": 20}})
    headers = [header(101, 1012), header(102, 1024)]
    original = confirm(events, headers, states=states)
    changed_states = deepcopy(states)
    changed_states[-1]["tick"] = 300
    changed = confirm(events, headers, states=changed_states)
    before = backward_asof(apply_availability(original, 12), 1050, 120)
    after = backward_asof(apply_availability(changed, 12), 1050, 120)
    assert before == after
    assert before["row"]["tick"] == 10

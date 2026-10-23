"""Confirm replayed state at evidenced block times, including quiet blocks."""
from __future__ import annotations

from collections import Counter

from .data import DataContractError, parse_integer


def _order(row: dict) -> tuple[int, int, int]:
    return (int(row["block_number"]), int(row.get("transaction_index", -1)),
            int(row.get("log_index", -1)))


def _coverage(manifest: dict, events: list[dict]) -> tuple[int, int]:
    if manifest.get("coverage_complete") is not True:
        raise DataContractError("Pool observations require complete log coverage")
    first, stop = int(manifest["block_start"]), int(manifest["block_end_exclusive"])
    cursor = first
    counts = Counter(int(row["block_number"]) for row in events)
    for chunk in manifest["chunks"]:
        left, right = int(chunk["from_block"]), int(chunk["to_block"])
        if left != cursor or right < left or right >= stop:
            raise DataContractError("Pool observation log coverage has a gap or overlap")
        if int(chunk["row_count"]) != sum(counts[n] for n in range(left, right + 1)):
            raise DataContractError("Pool observation chunk count mismatch")
        cursor = right + 1
    if cursor != stop or int(manifest["source_row_count"]) != len(events):
        raise DataContractError("Pool observation coverage/count incomplete")
    if any(not first <= int(row["block_number"]) < stop for row in events):
        raise DataContractError("Pool event lies outside observation coverage")
    return first, stop


def _changed(previous: dict, current: dict, event: dict) -> bool:
    fields = ("sqrt_price_x96", "tick", "liquidity", "fee_growth_global0_x128",
              "fee_growth_global1_x128", "fee_protocol0", "fee_protocol1")
    for key in fields:
        before, after = previous.get(key), current.get(key)
        before = None if before is None else parse_integer(before)
        after = None if after is None else parse_integer(after)
        if before != after:
            return True
    # An out-of-range Mint/Burn changes the tick map even if active L is unchanged.
    return event["event_type"] in {"Mint", "Burn"} and parse_integer(event.get("amount", 0)) != 0


def confirm_pool_observations(snapshot: dict, events: list[dict], states: list[dict],
                              manifest: dict, headers: list[dict]) -> tuple[list[dict], dict]:
    """Use only supplied real headers after processing every log through a block.

    Callers validate original log/header artifacts first. Missing empty-block
    headers produce no observation; backward joins must still enforce max age.
    Replay rows and their original times are left untouched.
    """
    first, stop = _coverage(manifest, events)
    ordered = sorted(events, key=_order)
    if len({_order(row) for row in ordered}) != len(ordered):
        raise DataContractError("Duplicate pool event ordering keys")
    by_header = {}
    for header in headers:
        number = int(header["block_number"])
        if number in by_header:
            raise DataContractError("Duplicate observation block header")
        by_header[number] = header
    for event in ordered:
        header = by_header.get(int(event["block_number"]))
        if header is None:
            raise DataContractError("Missing header for a pool event block")
        if (str(event["block_hash"]).lower(), float(event["timestamp"])) != (
                str(header["block_hash"]).lower(), float(header["timestamp"])):
            raise DataContractError("Pool event/header hash or timestamp mismatch")
    processed_states = [row for row in states if int(row["block_number"]) > int(snapshot["block_number"])]
    state_index = {_order(row): row for row in processed_states}
    if len(state_index) != len(processed_states):
        raise DataContractError("Duplicate replay state ordering keys")
    if set(state_index) != {_order(row) for row in ordered}:
        raise DataContractError("Every pool event needs its processed replay state")
    if ordered and int(snapshot["block_number"]) >= int(ordered[0]["block_number"]):
        raise DataContractError("Initial state must precede the first processed block")
    if int(snapshot["block_number"]) < first - 1:
        raise DataContractError("Log coverage does not bridge initialization")
    current = {key: value for key, value in snapshot.items() if key not in {"ticks", "bitmap_words"}}
    anchor_time = float(snapshot["timestamp"])
    change_time = snapshot.get("state_changed_at")
    change_block = snapshot.get("state_changed_block_number")
    last_event_at = None
    last_event_block = None
    known_at = float(snapshot.get("available_at", snapshot["timestamp"]))
    cursor = 0
    observations = []
    previous_time = None
    supported = {"Mint", "Burn", "Swap", "Flash", "SetFeeProtocol", "Collect",
                 "CollectProtocol", "IncreaseObservationCardinalityNext"}
    for number, header in sorted(by_header.items()):
        if number < max(first, int(snapshot["block_number"])) or number >= stop:
            continue
        at = float(header["timestamp"])
        if previous_time is not None and at <= previous_time:
            raise DataContractError("Observation block timestamps must increase")
        previous_time = at
        while cursor < len(ordered) and int(ordered[cursor]["block_number"]) <= number:
            event = ordered[cursor]
            if event["event_type"] not in supported:
                raise DataContractError("Unresolved pool event blocks state confirmation")
            processed = state_index[_order(event)]
            if (str(processed["block_hash"]).lower(), float(processed["timestamp"])) != (
                    str(event["block_hash"]).lower(), float(event["timestamp"])):
                raise DataContractError("Replay state/event identity mismatch")
            if _changed(current, processed, event):
                change_time, change_block = float(event["timestamp"]), int(event["block_number"])
            current = processed
            last_event_at, last_event_block = float(event["timestamp"]), int(event["block_number"])
            known_at = max(known_at, float(event.get("available_at", event["timestamp"])),
                           float(processed.get("available_at", processed["timestamp"])))
            cursor += 1
        if at < anchor_time or (change_time is not None and at < float(change_time)) or (last_event_at is not None and at < last_event_at):
            raise DataContractError("Observation predates the state being confirmed")
        observations.append({**current, "timestamp": at, "observed_at": at,
                             "available_at": max(at, known_at, float(header.get("available_at", at))),
                             "block_number": number, "block_hash": header["block_hash"],
                             "observed_block_number": number, "observed_block_hash": header["block_hash"],
                             "state_changed_at": change_time, "state_changed_block_number": change_block,
                             "state_anchor_at": anchor_time,
                             "last_event_at": last_event_at, "last_event_block_number": last_event_block,
                             "event_type": "BlockObservation", "transaction_hash": None,
                             "transaction_index": -1, "log_index": -1,
                             "observation_kind": "complete_pool_logs_through_block"})
    return observations, {"observations": len(observations), "covered_blocks": stop - first,
                          "blocks_without_observation_header": stop - first - len(observations),
                          "policy": "Sparse actual headers only; complete intervening pool logs required"}

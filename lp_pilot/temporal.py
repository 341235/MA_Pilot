"""UTC timestamps and joins that respect information availability."""
from __future__ import annotations

from bisect import bisect_right
from datetime import datetime
from decimal import Decimal
from math import isfinite, isnan
from typing import Any, Iterable, Mapping


class TimestampError(ValueError):
    """A timestamp is nonfinite or lacks an explicit timezone."""


def utc_timestamp(value: str | int | float | Decimal) -> float:
    """Convert an ISO UTC timestamp or already-normalized seconds to seconds."""
    if isinstance(value, str) and not value.strip().replace(".", "", 1).isdigit():
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise TimestampError("ISO timestamps must include an explicit timezone")
        return parsed.timestamp()
    result = float(value)
    if not isfinite(result):
        raise TimestampError("Timestamp must be finite")
    return result


def apply_availability(rows: Iterable[Mapping[str, Any]], delay_seconds: float = 0) -> list[dict]:
    """Respect observed availability; enforce a configured minimum source lag."""
    if delay_seconds < 0:
        raise ValueError("Availability delay must be nonnegative")
    result = []
    for row in rows:
        copied = dict(row)
        timestamp = utc_timestamp(copied["timestamp"])
        supplied = utc_timestamp(copied.get("available_at", timestamp))
        copied["timestamp"] = timestamp
        copied["available_at"] = max(timestamp + delay_seconds, supplied)
        result.append(copied)
    return result


def _asof_key(row: Mapping[str, Any]) -> tuple[float, int, int, int]:
    sequence = row.get("sequence", row.get("log_index", -1))
    return (float(row["timestamp"]), int(row.get("block_number") or 0),
            int(row.get("transaction_index") or 0),
            int(sequence) if sequence is not None else -1)


class AsOfIndex:
    """Reusable event-time lookup while retaining the input's iteration order.

    Construct once per immutable observation table. Event time determines the
    search position; availability is checked for every candidate, including
    delayed observations queried retrospectively with ``available_by``. Rows'
    timestamp, availability and ordering fields must remain unchanged afterward.
    """

    def __init__(self, rows: Iterable[Mapping[str, Any]]):
        self._rows = tuple(rows)
        # max() keeps the first eligible row for equal keys. Reverse input
        # position in this ascending index preserves that rule when walking back.
        ordered = sorted(((*_asof_key(row), -index), row)
                         for index, row in enumerate(self._rows)
                         if not isnan(float(row["timestamp"])))
        self._ordered = tuple(row for _, row in ordered)
        self._timestamps = tuple(float(row["timestamp"]) for row in self._ordered)
        self._available = tuple(float(row.get("available_at", row["timestamp"])) for row in self._ordered)

    def __iter__(self):
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def latest(self, at: float, *, available_by: float | None = None) -> Mapping[str, Any] | None:
        known_at = at if available_by is None else available_by
        if isnan(at):
            return None
        index = bisect_right(self._timestamps, at) - 1
        while index >= 0:
            if self._available[index] <= known_at:
                return self._ordered[index]
            index -= 1
        return None


def asof_index(rows: Iterable[Mapping[str, Any]]) -> AsOfIndex:
    """Build an index, or reuse one already supplied."""
    return rows if isinstance(rows, AsOfIndex) else AsOfIndex(rows)


def backward_asof(rows: Iterable[Mapping[str, Any]], at: float, max_age_seconds: float,
                  *, value_key: str | None = None, available_by: float | None = None) -> dict:
    """Latest event-time row whose event and availability both precede ``at``.

    A late-arriving old observation cannot replace a newer event-time observation.
    A stale latest observation is returned only as ``candidate`` for diagnostics.
    """
    if max_age_seconds < 0:
        raise ValueError("Maximum age must be nonnegative")
    known_at = at if available_by is None else available_by
    if isinstance(rows, AsOfIndex):
        row = rows.latest(at, available_by=known_at)
    else:
        eligible = [r for r in rows if float(r["timestamp"]) <= at
                    and float(r.get("available_at", r["timestamp"])) <= known_at]
        row = max(eligible, key=_asof_key) if eligible else None
    if row is None:
        return {"row": None, "value": None, "age_seconds": None, "status": "BLOCKED",
                "flags": ["MISSING_ASOF_OBSERVATION"]}
    age = at - float(row["timestamp"])
    if age > max_age_seconds:
        return {"row": None, "candidate": dict(row), "value": None, "age_seconds": age,
                "status": "BLOCKED", "flags": ["STALE_ASOF_OBSERVATION"]}
    return {"row": dict(row), "value": row.get(value_key) if value_key else None,
            "age_seconds": age, "status": "PASS", "flags": []}


def reference_asof(rows: Iterable[Mapping[str, Any]], at: float, max_age_seconds: float,
                   *, available_by: float | None = None) -> dict:
    """Apply the existing price-age limit to each observed conversion leg."""
    result = backward_asof(rows, at, max_age_seconds, value_key="price_usdc_per_eth", available_by=available_by)
    row = result.get("row") or result.get("candidate")
    if row is None:
        return {**result, "eth_age_seconds": None, "usdc_age_seconds": None}
    leg_fields = ("eth_timestamp", "usdc_timestamp", "eth_available_at", "usdc_available_at")
    if any(key in row for key in leg_fields) and any(row.get(key) is None for key in leg_fields):
        return {**result, "row": None, "candidate": row, "value": None, "status": "BLOCKED",
                "flags": [*result["flags"], "MISSING_REFERENCE_LEG"],
                "eth_age_seconds": None, "usdc_age_seconds": None}
    eth_time = float(row.get("eth_timestamp", row["timestamp"]))
    usdc_time = float(row.get("usdc_timestamp", row["timestamp"]))
    result.update(eth_age_seconds=at - eth_time, usdc_age_seconds=at - usdc_time)
    if result["row"] is None:
        return result
    known_at = at if available_by is None else available_by
    flags = list(result["flags"]) + list(row.get("reference_flags", []))
    if max(eth_time, usdc_time) > at:
        flags.append("FUTURE_REFERENCE_LEG")
    if max(float(row.get("eth_available_at", row.get("available_at", eth_time))),
           float(row.get("usdc_available_at", row.get("available_at", usdc_time)))) > known_at:
        flags.append("UNAVAILABLE_REFERENCE_LEG")
    if max(result["eth_age_seconds"], result["usdc_age_seconds"]) > max_age_seconds:
        flags.append("STALE_REFERENCE_LEG")
    if any(flag in flags for flag in ("FUTURE_REFERENCE_LEG", "UNAVAILABLE_REFERENCE_LEG", "STALE_REFERENCE_LEG")):
        return {**result, "row": None, "candidate": row, "value": None, "status": "BLOCKED", "flags": flags}
    return {**result, "flags": flags}


def benchmark_grid(entry: float, exit: float, step_seconds: int = 60) -> list[float]:
    if exit <= entry or step_seconds <= 0:
        raise ValueError("Benchmark needs a positive horizon and interval")
    points = [entry]
    while points[-1] + step_seconds < exit:
        points.append(points[-1] + step_seconds)
    points.append(exit)
    return points

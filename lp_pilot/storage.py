"""Parquet storage with exact on-chain integer values."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def json_value(value):
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


def write_json(path: str | Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_value(value), indent=2, sort_keys=True, allow_nan=False) + "\n")


def sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_table(path: str | Path, rows: list[dict], *, empty_columns: tuple[str, ...] = ()) -> None:
    """Store integers and Decimal as strings; do not infer uint160 as float64.

    Timestamp/order columns are safe int64, every other integer is a string.
    Nested diagnostics use JSON to keep schemas stable across partial runs.
    """
    safe = {"timestamp", "available_at", "decision_time", "execution_time", "end_time",
            "block_number", "transaction_index", "log_index", "tick", "tick_lower", "tick_upper"}
    converted = []
    for row in rows:
        output = {}
        for key, value in json_value(row).items():
            if isinstance(value, (list, dict)):
                value = json.dumps(value, sort_keys=True)
            elif isinstance(value, int) and not isinstance(value, bool) and key not in safe:
                value = str(value)
            output[key] = value
        converted.append(output)
    # Avoid float coercion for mixed numeric values.
    keys = sorted({key for row in converted for key in row})
    for key in keys:
        values = [r.get(key) for r in converted if r.get(key) is not None]
        if any(isinstance(v, str) for v in values):
            for row in converted:
                if row.get(key) is not None:
                    row[key] = str(row[key])
        for row in converted:
            row.setdefault(key, None)
    table = pa.Table.from_pylist(converted) if converted else pa.table({k: pa.array([], type=pa.string()) for k in empty_columns})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")


def read_table(path: str | Path) -> list[dict]:
    return pq.read_table(path).to_pylist()

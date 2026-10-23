"""Configuration and UTC interval boundaries."""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
import tomllib


def utc_seconds(value: str) -> int:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
        raise ValueError("Configuration timestamps must explicitly use UTC")
    return int(stamp.timestamp())


def load_config(path: str | Path) -> dict:
    path = Path(path).resolve()
    with path.open("rb") as handle:
        cfg = tomllib.load(handle)
    pilot, timing, pool = cfg["pilot"], cfg["timing"], cfg["pool"]
    start, end = utc_seconds(pilot["start"]), utc_seconds(pilot["end"])
    if start >= end or pilot["warmup_hours"] < 24:
        raise ValueError("Require a nonempty interval and at least 24h feature warmup")
    for key in ("decision_seconds", "holding_seconds", "capital_usdc"):
        if pilot[key] <= 0:
            raise ValueError(f"pilot.{key} must be positive")
    if len(pilot["widths"]) != 3 or sorted(pilot["widths"]) != pilot["widths"] or any(w <= 0 for w in pilot["widths"]):
        raise ValueError("Require three increasing positive range widths")
    if timing["benchmark_seconds"] <= 0 or any(v < 0 for v in timing.values()):
        raise ValueError("Timing parameters must be nonnegative and benchmark grid positive")
    if pool["chain_id"] != 1 or pool["decimals0"] != 6 or pool["decimals1"] != 18:
        raise ValueError("This small pilot supports Ethereum USDC(token0)/WETH(token1) only")
    if pool["tick_spacing"] != 10 or pool["fee_pips"] != 500:
        raise ValueError("This pilot supports the 0.05% pool only")
    if cfg["data"].get("source") != "rpc":
        raise ValueError("This pilot uses data.source='rpc'")
    if "acquisition" in cfg:
        for key, value in cfg["acquisition"].items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"acquisition.{key} must be positive")
        if cfg["acquisition"]["batch_size"] > 10 or cfg["acquisition"]["chunk_blocks"] > 10:
            raise ValueError("Alchemy acquisition batches and log intervals cannot exceed ten")
    cfg["_root"] = str(path.parent.parent)
    cfg["_config_path"] = str(path)
    cfg["_config_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return cfg


def required_interval(cfg: dict) -> tuple[int, int]:
    """Include warmup and the final holding, markout and latency windows."""
    p, t = cfg["pilot"], cfg["timing"]
    return (utc_seconds(p["start"]) - int(p["warmup_hours"] * 3600),
            utc_seconds(p["end"]) + p["holding_seconds"] + t["execution_delay_seconds"]
            + t["markout_seconds"] + max(t["block_delay_seconds"], t["reference_delay_seconds"]))


def decisions(cfg: dict) -> list[int]:
    p = cfg["pilot"]
    return list(range(utc_seconds(p["start"]), utc_seconds(p["end"]), p["decision_seconds"]))


def data_path(cfg: dict, key: str) -> Path:
    path = Path(cfg["data"][key])
    return path if path.is_absolute() else Path(cfg["_root"]) / path

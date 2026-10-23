"""Decode pool events and load Binance reference data."""
from __future__ import annotations

import csv
import io
import json
import math
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import requests

from .storage import read_table as read_rows, sha256 as sha256_file

POOL = "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"
EVENT_TOPICS = {
    "Initialize": "0x98636036cb66a9c19a37435efc1e90142190214e8abeb821bdba3f2990dd4c95",
    "Mint": "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde",
    "Burn": "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c",
    "Swap": "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",
    "Collect": "0x70935338e69775456a85ddef226c395fb668b63fa0115f5f20610b388e6ca9c0",
    "Flash": "0xbdbdb71d7860376ba52b25a5028beea23581364a40522f6bcfb86bb1f2dca633",
    "SetFeeProtocol": "0x973d8d92bb299f4af6ce49b52a8adb85ae46b9f214c4c4fc06ac77401237b133",
    "CollectProtocol": "0x596b573906218d3411850b26a6b437d6c4522fdb43d2d2386263f86d50b8b151",
    "IncreaseObservationCardinalityNext": "0xac49e518f90a358f652e4400164f05a5d8f7e35e7747279bc3a93dbf584e125a",
}
TOPIC_EVENTS = {value: key for key, value in EVENT_TOPICS.items()}
INTEGER_FIELDS = {
    "block_number", "transaction_index", "log_index", "amount0", "amount1",
    "sqrt_price_x96", "liquidity", "tick", "tick_lower", "tick_upper", "amount",
    "paid0", "paid1", "fee_protocol0_old", "fee_protocol1_old",
    "fee_protocol0_new", "fee_protocol1_new", "observation_cardinality_next_old",
    "observation_cardinality_next_new",
}


class DataContractError(ValueError):
    """Input data does not satisfy the data contract."""


def parse_integer(value: Any) -> int:
    """Reject floats, including apparently integral ones: precision may be lost."""
    if isinstance(value, bool) or isinstance(value, float):
        raise DataContractError("On-chain integers must not pass through floating point")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if re.fullmatch(r"-?(0|[1-9][0-9]*)", text):
        return int(text)
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", text):
        return int(text, 16)
    raise DataContractError("Invalid lossless integer encoding")


def utc_seconds(value: Any) -> float:
    if isinstance(value, (int, float, Decimal)):
        result = float(value)
    else:
        text = str(value).strip()
        try:
            result = float(text)
        except ValueError:
            if text.endswith(" UTC"):
                text = text[:-4] + "+00:00"
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                # Stored times without an offset are interpreted as UTC.
                parsed = parsed.replace(tzinfo=timezone.utc)
            result = parsed.timestamp()
    if not math.isfinite(result):
        raise DataContractError("Non-finite timestamp")
    return result


def _hex(value: Any, size: int | None = None) -> str:
    if isinstance(value, bytes):
        raw = value.hex()
    else:
        raw = str(value).strip().lower().removeprefix("0x")
    if len(raw) % 2 or not re.fullmatch(r"[0-9a-f]*", raw):
        raise DataContractError("Invalid hexadecimal data")
    if size is not None and len(raw) != 2 * size:
        raise DataContractError(f"Expected {size}-byte hexadecimal value")
    return "0x" + raw


def _word(value: str, *, signed: bool = False, bits: int = 256) -> int:
    raw = parse_integer("0x" + value.removeprefix("0x"))
    result = raw - (1 << 256) if signed and raw >= 1 << 255 else raw
    lower = -(1 << (bits - 1)) if signed else 0
    upper = (1 << (bits - int(signed))) - 1
    if not lower <= result <= upper:
        raise DataContractError("ABI integer width or sign extension mismatch")
    return result


def decode_pool_log(raw: dict[str, Any]) -> dict[str, Any]:
    """Decode pool events, retaining unknown topics for validation."""
    topics = raw.get("topics")
    if isinstance(topics, str):
        topics = json.loads(topics)
    if topics is None:
        topics = [raw[f"topic{i}"] for i in range(4) if raw.get(f"topic{i}") not in (None, "")]
    topics = [_hex(topic, 32) for topic in topics]
    if not topics:
        raise DataContractError("Pool log has no event signature")
    data = _hex(raw["data"])[2:]
    if len(data) % 64:
        raise DataContractError("Pool log data is not ABI word aligned")
    words = [data[index:index + 64] for index in range(0, len(data), 64)]
    event_type = TOPIC_EVENTS.get(topics[0], "Unknown")
    output: dict[str, Any] = {"event_type": event_type, "topic0": topics[0], "raw_data": "0x" + data}
    layouts = {
        "Initialize": [("sqrt_price_x96", False, 160), ("tick", True, 24)],
        "Mint": [("sender", None, 160), ("amount", False, 128), ("amount0", False, 256), ("amount1", False, 256)],
        "Burn": [("amount", False, 128), ("amount0", False, 256), ("amount1", False, 256)],
        "Swap": [("amount0", True, 256), ("amount1", True, 256), ("sqrt_price_x96", False, 160), ("liquidity", False, 128), ("tick", True, 24)],
        "Collect": [("recipient", None, 160), ("amount0", False, 128), ("amount1", False, 128)],
        "Flash": [("amount0", False, 256), ("amount1", False, 256), ("paid0", False, 256), ("paid1", False, 256)],
        "SetFeeProtocol": [("fee_protocol0_old", False, 8), ("fee_protocol1_old", False, 8), ("fee_protocol0_new", False, 8), ("fee_protocol1_new", False, 8)],
        "CollectProtocol": [("amount0", False, 128), ("amount1", False, 128)],
        "IncreaseObservationCardinalityNext": [("observation_cardinality_next_old", False, 16), ("observation_cardinality_next_new", False, 16)],
    }
    if event_type == "Unknown":
        return output
    expected_topics = 4 if event_type in {"Mint", "Burn", "Collect"} else 3 if event_type in {"Swap", "Flash", "CollectProtocol"} else 1
    if len(topics) != expected_topics or len(words) != len(layouts[event_type]):
        raise DataContractError(f"Incorrect {event_type} ABI field count")
    for (name, signed, bits), value in zip(layouts[event_type], words):
        if signed is None:
            _word(value, bits=160)
            output[name] = "0x" + value[-40:]
        else:
            output[name] = _word(value, signed=signed, bits=bits)
    if event_type in {"Mint", "Burn", "Collect"}:
        output.update(owner="0x" + topics[1][-40:], tick_lower=_word(topics[2], signed=True, bits=24), tick_upper=_word(topics[3], signed=True, bits=24))
    elif event_type in {"Swap", "Flash", "CollectProtocol"}:
        output.update(sender="0x" + topics[1][-40:], recipient="0x" + topics[2][-40:])
    return output


def normalize_event(row: dict[str, Any], pool_address: str = POOL) -> dict[str, Any]:
    aliases = {"blockNumber": "block_number", "blockHash": "block_hash", "block_time": "timestamp", "transactionHash": "transaction_hash", "tx_hash": "transaction_hash", "tx_index": "transaction_index", "transactionIndex": "transaction_index", "index": "log_index", "logIndex": "log_index", "contract_address": "pool_address", "address": "pool_address"}
    output = {aliases.get(key, key): value for key, value in row.items() if value not in (None, "")}
    for name in ("block_number", "block_hash", "timestamp", "transaction_hash", "transaction_index", "log_index"):
        if name not in output:
            raise DataContractError(f"Missing event field: {name}")
    address = _hex(output.get("pool_address", pool_address), 20)
    if address != pool_address.lower():
        raise DataContractError("Unexpected pool address")
    output["pool_address"] = address
    if "removed" in output:
        removed = output["removed"]
        if isinstance(removed, str) and removed.strip().lower() in {"true", "false"}:
            removed = removed.strip().lower() == "true"
        if not isinstance(removed, bool):
            raise DataContractError("Invalid removed-log boolean")
        if removed:
            raise DataContractError("Removed/reorged log present")
        output["removed"] = False
    if "event_type" not in output:
        output.update(decode_pool_log(row))
    for name in INTEGER_FIELDS & output.keys():
        output[name] = parse_integer(output[name])
    for name in ("block_hash", "transaction_hash"):
        output[name] = _hex(output[name], 32)
    output["timestamp"] = utc_seconds(output["timestamp"])
    if output["timestamp"] != int(output["timestamp"]):
        raise DataContractError("Ethereum block timestamps must be integer seconds")
    output["timestamp"] = int(output["timestamp"])
    if any(output[field] < 0 for field in ("block_number", "transaction_index", "log_index")):
        raise DataContractError("Negative event ordering key")
    return output


def order_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(events, key=lambda row: (row["block_number"], row["transaction_index"], row["log_index"]))
    seen: set[tuple[int, int]] = set()
    blocks: dict[int, tuple[str, int]] = {}
    transactions: dict[tuple[int, int], str] = {}
    for row in ordered:
        key = (row["block_number"], row["log_index"])
        if key in seen:
            raise DataContractError("Duplicate log identity")
        seen.add(key)
        block = (row["block_hash"], row["timestamp"])
        if row["block_number"] in blocks and blocks[row["block_number"]] != block:
            raise DataContractError("Inconsistent block hash or timestamp")
        blocks[row["block_number"]] = block
        transaction = (row["block_number"], row["transaction_index"])
        if transaction in transactions and transactions[transaction] != row["transaction_hash"]:
            raise DataContractError("Inconsistent transaction ordering")
        transactions[transaction] = row["transaction_hash"]
    if any(left["timestamp"] > right["timestamp"] for left, right in zip(ordered, ordered[1:])):
        raise DataContractError("Block time decreases in event order")
    return ordered


def binance_timestamp_seconds(value: Any) -> float:
    raw = parse_integer(value)
    # Epoch magnitude and calendar plausibility jointly distinguish ms and us.
    divisor = 1_000_000 if raw >= 100_000_000_000_000 else 1_000 if raw >= 100_000_000_000 else None
    if divisor is None:
        raise DataContractError("Binance timestamp must be milliseconds or microseconds")
    seconds = raw / divisor
    if not 1262304000 <= seconds < 4102444800:  # [2010, 2100)
        raise DataContractError("Binance timestamp outside plausible archive dates")
    return seconds


def load_binance_klines(path: str | Path, symbol: str) -> list[dict[str, Any]]:
    """Load spot one-minute CSV/ZIP. Close is available at the candle boundary."""
    path = Path(path)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".csv")]
            if len(names) != 1 or archive.getinfo(names[0]).file_size > 100_000_000:
                raise DataContractError("Unexpected Binance ZIP content")
            text = archive.read(names[0]).decode("utf-8-sig")
    else:
        text = path.read_text(encoding="utf-8-sig")
    output = []
    for values in csv.reader(io.StringIO(text)):
        if not values:
            continue
        if values[0] in ("open_time", "Open time"):
            continue
        if len(values) != 12:
            raise DataContractError("Binance spot kline needs 12 columns")
        open_time = binance_timestamp_seconds(values[0])
        close_time = binance_timestamp_seconds(values[6])
        if abs((close_time - open_time) - 60) > 0.0011:
            raise DataContractError("Expected one-minute kline duration")
        if open_time % 60 != 0:
            raise DataContractError("Kline open is not on a minute boundary")
        prices = [Decimal(value) for value in values[1:5]]
        if any(not value.is_finite() or value <= 0 for value in prices):
            raise DataContractError("Invalid kline price")
        output.append({"symbol": symbol, "open_time": open_time, "close_time": close_time, "timestamp": open_time + 60, "available_time": open_time + 60, "available_at": open_time + 60, "open": values[1], "high": values[2], "low": values[3], "close": values[4], "price": float(prices[3]), "volume": values[5], "trade_count": parse_integer(values[8]), "granularity_seconds": 60})
    output.sort(key=lambda row: row["open_time"])
    if len({row["open_time"] for row in output}) != len(output):
        raise DataContractError("Duplicate Binance minute")
    return output


@dataclass
class RequestBudget:
    max_requests: int = 20
    max_bytes: int = 25_000_000
    used_requests: int = 0
    used_bytes: int = 0

    def claim(self) -> None:
        if self.used_requests >= self.max_requests:
            raise DataContractError("Network request budget exhausted")
        self.used_requests += 1

    def consume(self, size: int) -> None:
        self.used_bytes += size
        if self.used_bytes > self.max_bytes:
            raise DataContractError("Network download byte budget exhausted")


def download_binance_daily(symbol: str, day: str | date, destination: str | Path, *, budget: RequestBudget | None = None, timeout: float = 10, session: Any = None) -> dict[str, Any]:
    """Download one daily minute archive and its SHA256 checksum."""
    if symbol not in {"ETHUSDT", "USDCUSDT"}:
        raise DataContractError("Pilot supports ETHUSDT and USDCUSDT")
    day = date.fromisoformat(str(day))
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    filename = f"{symbol}-1m-{day.isoformat()}.zip"
    target, checksum_target = destination / filename, destination / (filename + ".CHECKSUM")
    manifest_target = destination / (filename + ".manifest.json")
    cached = target.exists() and checksum_target.exists()
    previous = json.loads(manifest_target.read_text()) if cached and manifest_target.exists() else {}
    budget = budget or RequestBudget()
    session = session or requests.Session()
    base = f"https://data.binance.vision/data/spot/daily/klines/{symbol}/1m/{filename}"
    for url, output in ((base + ".CHECKSUM", checksum_target), (base, target)):
        if output.exists():
            continue
        budget.claim()
        try:
            response = session.get(url, timeout=timeout, stream=True)
            response.raise_for_status()
            chunks = []
            for chunk in response.iter_content(65536):
                budget.consume(len(chunk))
                chunks.append(chunk)
            output.write_bytes(b"".join(chunks))
        except requests.RequestException as exc:
            raise DataContractError(f"Binance public download failed ({type(exc).__name__})") from None
    expected = checksum_target.read_text().split()
    if not expected or not re.fullmatch(r"[0-9a-fA-F]{64}", expected[0]):
        raise DataContractError("Malformed Binance checksum sidecar")
    digest = sha256_file(target)
    if digest != expected[0].lower():
        raise DataContractError("Binance archive checksum mismatch")
    rows = load_binance_klines(target, symbol)
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()
    if len(rows) != 1440 or rows[0]["open_time"] != day_start or rows[-1]["open_time"] != day_start + 86340:
        raise DataContractError("Binance daily archive has incomplete minute coverage")
    verified_at = datetime.now(timezone.utc).isoformat()
    # Rechecking the cache is not a new retrieval.
    retrieved_at = previous.get("retrieved_at") if cached else verified_at
    if retrieved_at is None:
        retrieved_at = datetime.fromtimestamp(target.stat().st_mtime, timezone.utc).isoformat()
    result = {"source": "Binance Public Data spot klines", "url": base, "symbol": symbol, "path": str(target), "retrieved_at": retrieved_at, "verified_at": verified_at, "cached": cached, "sha256": digest, "checksum_verified": True, "row_count": len(rows), "coverage_start": day_start, "coverage_end": day_start + 86400, "granularity_seconds": 60, "timestamp_unit": "microseconds" if day >= date(2025, 1, 1) else "milliseconds", "download_bytes": target.stat().st_size}
    manifest_target.write_text(json.dumps(result, indent=2) + "\n")
    return result


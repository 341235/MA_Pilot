"""Historical Binance aggregate trade prices for a bounded reference comparison."""
from __future__ import annotations

from bisect import bisect_right
from contextlib import contextmanager
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import io
import json
import math
from pathlib import Path
import re
import tempfile
import zipfile

import requests

from .data import DataContractError, RequestBudget, parse_integer, sha256_file, utc_seconds


def _period(day: str | date) -> tuple[date, int, int, int]:
    day = date.fromisoformat(str(day))
    start = int(datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp())
    scale = 1_000_000 if day >= date(2025, 1, 1) else 1000
    return day, start, start + 86400, scale


def _symbol(symbol: str) -> None:
    if symbol not in {"ETHUSDT", "USDCUSDT"}:
        raise DataContractError("Fine reference supports ETHUSDT and USDCUSDT")


def _positive_decimal(value: str, name: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise DataContractError(f"Invalid aggregate trade {name}") from None
    if (not result.is_finite() or result <= 0 or not math.isfinite(float(result))
            or float(result) <= 0):
        raise DataContractError(f"Invalid aggregate trade {name}")
    return result


def _boolean(value: str) -> bool:
    if value.strip().lower() not in {"true", "false"}:
        raise DataContractError("Invalid aggregate trade boolean")
    return value.strip().lower() == "true"


@contextmanager
def _csv_stream(path: Path):
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            yield handle
        return
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if (len(names) != 1 or not names[0].endswith(".csv")
                    or archive.getinfo(names[0]).file_size > 100_000_000):
                raise DataContractError("Unexpected aggregate trade ZIP content")
            with archive.open(names[0]) as raw:
                with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as handle:
                    yield handle
    except (zipfile.BadZipFile, UnicodeDecodeError, OSError) as error:
        raise DataContractError(f"Invalid aggregate trade archive ({type(error).__name__})") from None


def _iter_aggtrades(path: Path, symbol: str, day: str | date):
    _symbol(symbol)
    _, start, stop, scale = _period(day)
    previous_id = previous_time = None
    with _csv_stream(path) as handle:
        for line, values in enumerate(csv.reader(handle), 1):
            if not values:
                continue
            # Official Spot archives normally have no header in the 2024 files.
            label = re.sub(r"[^a-z]", "", values[0].lower())
            if line == 1 and label in {"aggtradeid", "aggregatetradeid"} and len(values) == 8:
                continue
            if len(values) != 8:
                raise DataContractError("Binance Spot aggTrades needs eight columns")
            aggregate_id, first_id, last_id, raw_time = (
                parse_integer(values[index]) for index in (0, 3, 4, 5)
            )
            if min(aggregate_id, first_id, last_id) < 0 or first_id > last_id:
                raise DataContractError("Invalid aggregate trade identity range")
            if not start * scale <= raw_time < stop * scale:
                raise DataContractError("Aggregate trade timestamp unit or UTC archive day mismatch")
            if previous_id is not None and aggregate_id <= previous_id:
                raise DataContractError("Duplicate or decreasing aggregate trade ID")
            if previous_time is not None and raw_time < previous_time:
                raise DataContractError("Aggregate trade timestamp decreases")
            price = _positive_decimal(values[1], "price")
            quantity = _positive_decimal(values[2], "quantity")
            previous_id, previous_time = aggregate_id, raw_time
            yield {
                "symbol": symbol, "aggregate_trade_id": aggregate_id,
                "first_trade_id": first_id, "last_trade_id": last_id,
                "price": str(price), "quantity": str(quantity),
                "timestamp": raw_time / scale, "source_timestamp_raw": raw_time,
                "timestamp_unit": "microseconds" if scale == 1_000_000 else "milliseconds",
                "buyer_is_maker": _boolean(values[6]), "is_best_match": _boolean(values[7]),
                "sequence": aggregate_id,
            }


def load_aggtrades(path: str | Path, symbol: str, day: str | date, *,
                   start=None, end=None) -> list[dict]:
    """Validate the entire daily file; return the requested half-open subperiod."""
    _, day_start, day_stop, _ = _period(day)
    first = day_start if start is None else utc_seconds(start)
    stop = day_stop if end is None else utc_seconds(end)
    if first >= stop:
        raise DataContractError("Aggregate trade selection must have a positive duration")
    return [row for row in _iter_aggtrades(Path(path), symbol, day)
            if first <= row["timestamp"] < stop]


def _download(url: str, *, budget: RequestBudget, timeout: float, session) -> bytes:
    budget.claim()
    response = None
    try:
        response = session.get(url, timeout=timeout, stream=True, allow_redirects=False)
        response.raise_for_status()
        if response.status_code != 200:
            raise DataContractError("Binance aggregate trade download must return HTTP 200")
        length = response.headers.get("Content-Length")
        if length is not None and int(length) > budget.max_bytes - budget.used_bytes:
            raise DataContractError("Aggregate trade file exceeds remaining download byte budget")
        chunks = []
        for chunk in response.iter_content(65536):
            budget.consume(len(chunk))
            chunks.append(chunk)
        return b"".join(chunks)
    except requests.RequestException as error:
        raise DataContractError(f"Binance aggregate trade download failed ({type(error).__name__})") from None
    finally:
        if response is not None:
            response.close()


def _checksum(content: bytes, filename: str) -> str:
    try:
        fields = content.decode("ascii").split()
    except UnicodeDecodeError:
        raise DataContractError("Malformed aggregate trade checksum sidecar") from None
    if (len(fields) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0])
            or fields[1].lstrip("*") != filename):
        raise DataContractError("Malformed aggregate trade checksum sidecar")
    return fields[0].lower()


def download_aggtrades(symbol: str, day: str | date, destination: str | Path, *,
                       budget: RequestBudget | None = None, timeout: float = 10,
                       session=None) -> dict:
    """Cache one public daily ZIP and its checksum without replacing existing data."""
    _symbol(symbol)
    day, start, stop, scale = _period(day)
    budget = budget if budget is not None else RequestBudget()
    session = session if session is not None else requests.Session()
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    filename = f"{symbol}-aggTrades-{day.isoformat()}.zip"
    target = destination / filename
    sidecar = destination / (filename + ".CHECKSUM")
    manifest_path = destination / (filename + ".manifest.json")
    url = f"https://data.binance.vision/data/spot/daily/aggTrades/{symbol}/{filename}"
    cached = target.exists()
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    checksum_bytes = sidecar.read_bytes() if sidecar.exists() else _download(
        url + ".CHECKSUM", budget=budget, timeout=timeout, session=session,
    )
    expected = _checksum(checksum_bytes, filename)
    temporary = None
    try:
        source = target
        if not cached:
            content = _download(url, budget=budget, timeout=timeout, session=session)
            with tempfile.NamedTemporaryFile(dir=destination, suffix=".zip", delete=False) as handle:
                handle.write(content)
                temporary = Path(handle.name)
            source = temporary
        digest = sha256_file(source)
        if digest != expected:
            raise DataContractError("Binance aggregate trade archive checksum mismatch")
        count = gaps = 0
        first_time = last_time = first_id = last_id = None
        for row in _iter_aggtrades(source, symbol, day):
            if count == 0:
                first_time, first_id = row["timestamp"], row["aggregate_trade_id"]
            if last_id is not None and row["aggregate_trade_id"] != last_id + 1:
                gaps += 1
            last_time, last_id = row["timestamp"], row["aggregate_trade_id"]
            count += 1
        if not count:
            raise DataContractError("Empty aggregate trade daily archive")
        now = datetime.now(timezone.utc).isoformat()
        retrieved_at = previous.get("retrieved_at") if cached else now
        if cached and previous.get("sha256") not in {None, digest}:
            raise DataContractError("Cached aggregate trade provenance checksum mismatch")
        result = {
            "source": "Binance Public Data Spot aggTrades", "url": url,
            "checksum_url": url + ".CHECKSUM", "symbol": symbol,
            "path": str(target), "day": day.isoformat(),
            "coverage_start": start, "coverage_end": stop,
            "first_trade_timestamp": first_time, "last_trade_timestamp": last_time,
            "first_aggregate_trade_id": first_id, "last_aggregate_trade_id": last_id,
            "row_count": count, "aggregate_id_gap_count": gaps,
            "sha256": digest, "checksum_verified": True,
            "timestamp_unit": "microseconds" if scale == 1_000_000 else "milliseconds",
            "download_bytes": source.stat().st_size, "cached": cached,
            "retrieved_at": retrieved_at, "verified_at": now,
            "availability": "Trade time plus separately configured assumed delay; retrieval is not historical availability",
            "price_type": "executed aggregate trade price",
        }
        if gaps:
            result["quality_flags"] = ["AGGREGATE_TRADE_ID_GAPS"]
        if temporary is not None:
            temporary.replace(target)
            temporary = None
        if not sidecar.exists():
            sidecar.write_bytes(checksum_bytes)
        # The first retrieval record remains evidence; a cache check is returned
        # to the caller rather than rewriting that historical record.
        if not manifest_path.exists():
            manifest_path.write_text(json.dumps(result, indent=2) + "\n")
        return result
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _ordered_quotes(rows: list[dict], symbol: str, delay: float) -> list[dict]:
    output = []
    previous = None
    for row in rows:
        if row.get("symbol", symbol) != symbol:
            raise DataContractError("Unexpected symbol in aggregate trade conversion")
        timestamp = utc_seconds(row["timestamp"])
        sequence = parse_integer(row["aggregate_trade_id"])
        key = (timestamp, sequence)
        if previous is not None and (key <= previous or sequence <= previous[1]):
            raise DataContractError("Aggregate conversion input must retain time/ID order without duplicates")
        previous = key
        price = _positive_decimal(str(row["price"]), "price")
        available_at = max(timestamp + delay, utc_seconds(row.get("available_at", timestamp)))
        output.append({"aggregate_trade_id": sequence, "price": str(price),
                       "timestamp": timestamp, "available_at": available_at})
    return output


def convert_aggtrades(eth: list[dict], usdc: list[dict], *, delay_seconds: float = 1,
                      max_age_seconds: float = 120) -> tuple[list[dict], dict]:
    """Backward conversion at ETH trade time, with both source times retained."""
    if not math.isfinite(delay_seconds) or delay_seconds < 0:
        raise DataContractError("Aggregate reference delay must be finite and nonnegative")
    if not math.isfinite(max_age_seconds) or max_age_seconds < 0:
        raise DataContractError("Aggregate reference maximum age must be finite and nonnegative")
    eth = _ordered_quotes(eth, "ETHUSDT", delay_seconds)
    usdc = _ordered_quotes(usdc, "USDCUSDT", delay_seconds)
    timestamps = [row["timestamp"] for row in usdc]
    output = []
    diagnostics = {"eth_trade_count": len(eth), "usdc_trade_count": len(usdc),
                   "converted_count": 0, "missing_usdc_count": 0,
                   "unavailable_usdc_count": 0, "stale_usdc_count": 0,
                   "asynchronous_leg_count": 0, "delay_seconds": delay_seconds,
                   "max_age_seconds": max_age_seconds,
                   "price_type": "executed trade prices, not order-book midprices"}
    for trade in eth:
        at = trade["timestamp"]
        index = bisect_right(timestamps, at) - 1
        if index < 0:
            diagnostics["missing_usdc_count"] += 1
            continue
        while index >= 0 and usdc[index]["available_at"] > trade["available_at"]:
            index -= 1
        if index < 0:
            diagnostics["unavailable_usdc_count"] += 1
            continue
        conversion = usdc[index]
        age = at - conversion["timestamp"]
        if age > max_age_seconds:
            diagnostics["stale_usdc_count"] += 1
            continue
        flags = ["EXECUTED_TRADE_PRICE_REFERENCE"]
        if age:
            flags.append("ASYNCHRONOUS_REFERENCE_LEGS")
            diagnostics["asynchronous_leg_count"] += 1
        price = Decimal(trade["price"]) / Decimal(conversion["price"])
        output.append({
            "timestamp": at, "available_at": max(trade["available_at"], conversion["available_at"]),
            "price_usdc_per_eth": float(price), "eth_usdt": float(trade["price"]),
            "usdc_usdt": float(conversion["price"]),
            "eth_timestamp": at, "usdc_timestamp": conversion["timestamp"],
            "eth_available_at": trade["available_at"],
            "usdc_available_at": conversion["available_at"],
            "eth_age_seconds": 0.0, "usdc_age_seconds": age,
            "eth_aggregate_trade_id": trade["aggregate_trade_id"],
            "usdc_aggregate_trade_id": conversion["aggregate_trade_id"],
            "sequence": trade["aggregate_trade_id"],
            "reference_granularity_seconds": None,
            "reference_source": "Binance Spot aggTrades ETHUSDT / USDCUSDT",
            "reference_flags": flags,
        })
    diagnostics["converted_count"] = len(output)
    return output, diagnostics


def fetch_fine_references(cfg: dict) -> dict:
    """Fetch the fixed comparison day and preceding day within the normal budget."""
    root = Path(cfg["_root"])
    day = date.fromisoformat(cfg.get("feature_validation", {}).get("day", "2024-01-15"))
    directory = root / cfg["data"].get("fine_reference_dir", "data/cache/binance/aggTrades")
    budget = RequestBudget(cfg["network"]["max_requests"], cfg["network"]["max_download_bytes"])
    sources = []
    for source_day in (day - timedelta(days=1), day):
        for symbol in ("ETHUSDT", "USDCUSDT"):
            sources.append(download_aggtrades(symbol, source_day, directory, budget=budget,
                                              timeout=cfg["network"]["timeout_seconds"]))
    report = {"status": "PASS", "comparison_day": day.isoformat(), "sources": sources,
              "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
              "requests": budget.used_requests, "download_bytes": budget.used_bytes}
    return report

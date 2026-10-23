"""One fixed-day, cache-only check of reference-price construction."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import argparse
import json

from .config import data_path, load_config
from .data import DataContractError, load_binance_klines
from .feature_validation import (METRICS, _fine_quotes, _load_run, _number, _utc,
                                 compare_measurements, proxy_measurements)
from .features import historical_markout
from .fine_reference import _iter_aggtrades
from .storage import sha256, write_table
from .temporal import apply_availability, asof_index, reference_asof


def aggregate_minutes(rows, symbol: str) -> list[dict]:
    """Sorted trades in [u,u+60); equal milliseconds use the larger aggregate ID."""
    minutes, previous = {}, None
    for row in rows:
        raw, identity = int(row["source_timestamp_raw"]), int(row["aggregate_trade_id"])
        key = raw, identity
        if previous is not None and key <= previous:
            raise DataContractError("Minute aggregation requires increasing (time, aggregate ID)")
        previous = key
        opened = raw // 60_000 * 60  # These fixed 2024 archives use milliseconds.
        count = minutes.get(opened, {}).get("aggregate_trade_count", 0) + 1
        minutes[opened] = {"symbol": symbol, "open_time": opened, "timestamp": opened + 60,
                           "candle_completed_at": opened + 60,
                           "last_trade_timestamp": raw / 1000,
                           "last_aggregate_trade_id": identity, "source_timestamp_raw": raw,
                           "close": row["price"], "aggregate_trade_count": count}
    return list(minutes.values())


def compare_minutes(own: list[dict], klines: list[dict]) -> dict:
    """Exact decimal price equality and time assignment are separate checks."""
    trades, candles = ({row["open_time"]: row for row in rows} for rows in (own, klines))
    rows = []
    for opened in sorted(trades.keys() & candles.keys()):
        trade, candle = trades[opened], candles[opened]
        difference = Decimal(trade["close"]) - Decimal(candle["close"])
        boundary = Decimal(str(opened)) + 60
        time_ok = (Decimal(str(trade["timestamp"])) == Decimal(str(candle["timestamp"])) == boundary
                   and Decimal(str(candle["close_time"])) == boundary - Decimal("0.001")
                   and opened <= trade["last_trade_timestamp"] < opened + 60)
        rows.append({**trade, "kline_close": candle["close"], "kline_close_time": candle["close_time"],
                     "prices_equal": difference == 0, "raw_close_difference": str(difference),
                     "close_difference_bps": str(difference / Decimal(candle["close"]) * 10_000),
                     "time_mapping_correct": time_ok})
    return {"compared_minutes": len(rows), "exact_price_matches": sum(r["prices_equal"] for r in rows),
            "price_mismatch_count": sum(not r["prices_equal"] for r in rows),
            "time_mapping_error_count": sum(not r["time_mapping_correct"] for r in rows),
            "missing_trade_minutes": sorted(candles.keys() - trades.keys()),
            "missing_kline_minutes": sorted(trades.keys() - candles.keys()), "rows": rows}


def make_minute_references(legs: dict, delay_seconds: float) -> list[dict]:
    eth, usdc = ({row["timestamp"]: row for row in legs[symbol]} for symbol in ("ETHUSDT", "USDCUSDT"))
    return [{"timestamp": stamp, "available_at": stamp + delay_seconds,
             "price_usdc_per_eth": Decimal(eth[stamp]["close"]) / Decimal(usdc[stamp]["close"]),
             "ethusdt": eth[stamp]["close"], "usdcusdt": usdc[stamp]["close"],
             "eth_timestamp": stamp, "usdc_timestamp": stamp,
             "eth_available_at": stamp + delay_seconds, "usdc_available_at": stamp + delay_seconds,
             "eth_last_trade_timestamp": eth[stamp]["last_trade_timestamp"],
             "usdc_last_trade_timestamp": usdc[stamp]["last_trade_timestamp"],
             "reference_granularity_seconds": 60, "reference_price_kind": "closed_minute_candle"}
            for stamp in sorted(eth.keys() & usdc.keys())]


def common_markouts(decisions, events, references: dict, cfg: dict) -> dict:
    """Recompute all variants on the same intersection with original USDC weights."""
    timing, rows, totals = cfg["timing"], [], Counter()
    swaps = apply_availability(events, timing["block_delay_seconds"])
    arguments = {"lookback_seconds": timing["lookback_seconds"], "horizon_seconds": timing["markout_seconds"],
                 "max_price_age_seconds": timing["max_price_age_seconds"],
                 "reference_delay_seconds": timing["reference_delay_seconds"]}
    for decision in decisions:
        eligible = {}
        for event in swaps:
            stamp = float(event["timestamp"])
            if (event["event_type"].lower() != "swap" or not decision - timing["lookback_seconds"] < stamp <= decision
                    or float(event["available_at"]) > decision
                    or stamp + timing["markout_seconds"] + timing["reference_delay_seconds"] > decision
                    or not int(event["amount0"]) or not int(event["amount1"])):
                continue
            identity = f"{event['transaction_hash']}:{event.get('log_index', 0)}"
            if identity in eligible:
                raise DataContractError("Duplicate Swap identity in reference diagnosis")
            eligible[identity] = event
        valid = {label: {identity for identity, event in eligible.items()
                         if historical_markout([event], refs, decision, **arguments)["historical_markout_5m_bps"] is not None}
                 for label, refs in references.items()}
        common = set.intersection(*valid.values())
        common_events = [event for identity, event in eligible.items() if identity in common]
        volume = sum((Decimal(abs(int(event["amount0"]))) / 10**6 for event in common_events), Decimal(0))
        row = {"decision_time": decision, "candidate_mature_swaps": len(eligible), "common_valid_swaps": len(common),
               "common_quote_volume_usdc": str(volume), "common_identities": sorted(common),
               "valid_counts": {label: len(keys) for label, keys in valid.items()},
               "excluded_identities": {label: sorted(eligible.keys() - keys) for label, keys in valid.items()},
               "lost_to_intersection": {label: sorted(keys - common) for label, keys in valid.items()}}
        for label, refs in references.items():
            row[f"{label}_markout_bps"] = historical_markout(common_events, refs, decision, **arguments)["historical_markout_5m_bps"]
            totals[f"{label}_valid_swaps"] += len(valid[label])
        totals.update(candidate_mature_swaps=len(eligible), common_valid_swaps=len(common))
        rows.append(row)
    return {"totals": dict(totals), "decision_rows": rows,
            "membership_identical": all(all(not keys for keys in row["lost_to_intersection"].values()) for row in rows),
            "weighting": "Production historical_markout on identical events; abs(amount0)/1e6 USDC, no per-trade float recombination"}


def compare_variants(measured: dict, common: dict, decisions: list) -> tuple[dict, dict]:
    comparisons, masks = {}, {}
    for metric in METRICS:
        values = {label: [{"decision_time": row["decision_time"], metric: row[f"{label}_markout_bps"]}
                          for row in common["decision_rows"]] if metric == "historical_markout_5m_bps" else rows
                  for label, rows in measured.items()}
        lookup = {label: {row["decision_time"]: row.get(metric) for row in rows} for label, rows in values.items()}
        excluded = [{"decision_time": stamp, "missing_variants": [label for label, rows in lookup.items() if rows.get(stamp) is None]}
                    for stamp in decisions if any(rows.get(stamp) is None for rows in lookup.values())]
        eligible = set(decisions) - {row["decision_time"] for row in excluded}
        masks[metric] = {"included": sorted(eligible), "excluded": excluded}
        for left, right in (("A", "B"), ("A", "C"), ("B", "C")):
            pair = [[row for row in values[label] if row["decision_time"] in eligible] for label in (left, right)]
            comparisons.setdefault(f"{left}/{right}", {})[metric] = compare_measurements(*pair, metric)
    return comparisons, masks


def _cached(path: Path, symbol: str, day: date) -> None:
    sidecar, record = Path(str(path) + ".CHECKSUM"), Path(str(path) + ".manifest.json")
    manifest = json.loads(record.read_text())
    digest, fields = sha256(path), sidecar.read_text().split()
    if (len(fields) != 2 or fields[0].lower() != digest or fields[1].lstrip("*") != path.name
            or manifest.get("sha256") != digest or manifest.get("checksum_verified") is not True
            or manifest.get("symbol") != symbol or not manifest.get("retrieved_at")
            or ("day" in manifest and manifest["day"] != str(day))):
        raise DataContractError(f"Cached reference provenance mismatch: {path.name}")


def _trace(refs, at, known, timing, minute_lookup) -> dict:
    quote = reference_asof(refs, at, timing["max_price_age_seconds"], available_by=known)
    row = quote.get("row")
    if row is None:
        return {"target_time": _utc(at), "known_by": _utc(known), "flags": quote["flags"]}
    output = {"target_time": _utc(at), "known_by": _utc(known), "price_usdc_per_eth": str(quote["value"]),
              "available_at": _utc(row["available_at"]), "flags": quote["flags"], "legs": {}}
    for leg, symbol, minute_key in (("eth", "ETHUSDT", "ethusdt"), ("usdc", "USDCUSDT", "usdcusdt")):
        stamp = float(row[f"{leg}_timestamp"])
        minute = minute_lookup[symbol].get(stamp) if int(row.get("reference_granularity_seconds") or 0) == 60 else None
        actual = minute["last_trade_timestamp"] if minute else stamp
        output["legs"][leg] = {"price_usdt": str(row.get(f"{leg}_usdt", row.get(minute_key))),
                               "observation_time": _utc(stamp), "available_at": _utc(row[f"{leg}_available_at"]),
                               "age_seconds": at - stamp, "actual_last_trade_time": _utc(actual),
                               "actual_trade_age_seconds": at - actual,
                               "aggregate_trade_id": minute["last_aggregate_trade_id"] if minute else row.get(f"{leg}_aggregate_trade_id"),
                               "observation_kind": "candle completion" if minute else "executed trade"}
    return output


def diagnose_references(cfg: dict) -> dict:
    if cfg.get("feature_validation", {}).get("day") != "2024-01-15":
        raise DataContractError("This diagnosis is restricted to 2024-01-15 UTC")
    root, timing = Path(cfg["_root"]), cfg["timing"]
    day = date(2024, 1, 15)
    start = datetime(2024, 1, 15, tzinfo=timezone.utc).timestamp()
    history, end = start - 3900, start + 24 * 3600
    decisions = [start + hour * 3600 for hour in range(24)]
    tables, _ = _load_run(cfg)
    legs, audits = {}, {}
    for symbol in ("ETHUSDT", "USDCUSDT"):
        aggregated, klines = [], []
        for archive_day in (date(2024, 1, 14), day):
            directory = data_path(cfg, "reference_dir")
            path = directory / "aggTrades" / f"{symbol}-aggTrades-{archive_day}.zip"
            _cached(path, symbol, archive_day)
            trades = (row for row in _iter_aggtrades(path, symbol, archive_day) if history <= row["timestamp"] < end)
            aggregated.extend(aggregate_minutes(trades, symbol))
            path = directory / f"{symbol}-1m-{archive_day}.zip"
            _cached(path, symbol, archive_day)
            klines.extend(row for row in load_binance_klines(path, symbol) if history <= row["open_time"] < end)
        legs[symbol] = aggregated
        audits[symbol] = compare_minutes(aggregated, klines)
    minute_b = make_minute_references(legs, timing["reference_delay_seconds"])
    # A remains the saved production-minute path. Check its prices and timing
    # independently against the just-reconstructed closes before comparing features.
    minute_a = [row for row in tables["reference_prices"] if history < float(row["timestamp"]) <= end]
    b_lookup = {row["timestamp"]: row for row in minute_b}
    saved_differences = []
    for row in minute_a:
        other = b_lookup.get(row["timestamp"])
        fields = ("eth_timestamp", "usdc_timestamp", "eth_available_at", "usdc_available_at", "available_at")
        if (other is None or Decimal(str(row["price_usdc_per_eth"])) != other["price_usdc_per_eth"]
                or any(Decimal(row[key]) != Decimal(other[key]) for key in ("ethusdt", "usdcusdt"))
                or any(float(row[key]) != other[key] for key in fields)):
            saved_differences.append(row["timestamp"])
    fine, _ = _fine_quotes(cfg, day, history, decisions[-1] + 60)
    # Counterexample check: the last converted ETH tick can precede the final
    # USDC trade of its minute. This is deliberately NOT how B is constructed.
    last_converted = {}
    for row in fine:
        last_converted[int(row["timestamp"] // 60) * 60 + 60] = row
    construction = []
    for stamp, row in last_converted.items():
        other = b_lookup.get(stamp)
        if other and other["usdc_last_trade_timestamp"] > other["eth_last_trade_timestamp"]:
            ratio = Decimal(str(row["eth_usdt"])) / Decimal(str(row["usdc_usdt"]))
            construction.append({"minute_open": _utc(stamp - 60), "separate_close_price": str(other["price_usdc_per_eth"]),
                                 "last_converted_tick_price_before_float": str(ratio),
                                 "difference_bps": str((ratio / other["price_usdc_per_eth"] - 1) * 10_000),
                                 "eth_trade_time": _utc(row["eth_timestamp"]), "usdc_at_eth_time": _utc(row["usdc_timestamp"]),
                                 "usdc_final_trade_time": _utc(other["usdc_last_trade_timestamp"]),
                                 "usdc_at_eth_price": str(row["usdc_usdt"]), "usdc_final_price": other["usdcusdt"]})
    rounding = max(abs(Decimal(str(row["price_usdc_per_eth"])) - Decimal(str(row["eth_usdt"])) / Decimal(str(row["usdc_usdt"]))) for row in fine)
    refs = {"A": asof_index(apply_availability(minute_a, timing["reference_delay_seconds"])),
            "B": asof_index(minute_b), "C": asof_index(fine)}
    del fine
    events = [row for row in tables["events"] if history <= float(row["timestamp"]) <= decisions[-1]]
    measured = {label: proxy_measurements(decisions, events, reference, tables["pool_observations"], cfg,
                                        block_delay_seconds=timing["block_delay_seconds"])
                for label, reference in refs.items()}
    common = common_markouts(decisions, events, refs, cfg)
    comparisons, common_decisions = compare_variants(measured, common, decisions)
    minute_lookup = {symbol: {row["timestamp"]: row for row in rows} for symbol, rows in legs.items()}
    examples = []
    available_events = apply_availability(events, timing["block_delay_seconds"])
    for decision in (decisions[0], decisions[12], decisions[-1]):
        trace = {label: _trace(reference, decision, decision, timing, minute_lookup) for label, reference in refs.items()}
        common_ids = next(row["common_identities"] for row in common["decision_rows"] if row["decision_time"] == decision)
        candidates = sorted((row for row in available_events if f"{row['transaction_hash']}:{row.get('log_index', 0)}" in common_ids),
                            key=lambda row: (float(row["timestamp"]), int(row.get("transaction_index", 0)), int(row.get("log_index", 0))))
        example = {"decision_time": _utc(decision), "decision_references": trace}
        if candidates:
            event = candidates[0]
            at = float(event["timestamp"])
            example["swap"] = {"transaction_hash": event["transaction_hash"], "log_index": event["log_index"],
                               "trade_time": _utc(at), "direction": "ETH buy" if int(event["amount1"]) < 0 else "ETH sell",
                               "quote_volume_usdc": str(Decimal(abs(int(event["amount0"]))) / 10**6),
                               "endpoints": {label: {name: _trace(reference, stamp, decision, timing, minute_lookup)
                                                     for name, stamp in (("initial", at), ("target", at + timing["markout_seconds"]))}
                                             for label, reference in refs.items()}}
        examples.append(example)
    missing_a = sorted(b_lookup.keys() - {row["timestamp"] for row in minute_a})
    minutes_agree = (not saved_differences and not missing_a
                     and all(not audit[key] for audit in audits.values() for key in
                             ("price_mismatch_count", "time_mapping_error_count", "missing_trade_minutes", "missing_kline_minutes"))
                     and all(pair["left"] == pair["right"] for value in comparisons["A/B"].values() for pair in value["pairs"]))
    for audit in audits.values():
        audit.pop("rows")
    report = {"status": "WARN" if minutes_agree else "FAIL",
              "minutes_agree": minutes_agree, "day": str(day), "network_requests": 0,
              "minute_audits": audits, "saved_A_vs_reconstructed_B_differences": saved_differences,
              "minute_reference_missing_from_A": missing_a,
              "variant_counts": {label: {key: sum(int(row[key]) for row in rows) for key in
                                          ("markout_mature_count", "markout_immature_count", "markout_missing_reference_count")}
                                 for label, rows in measured.items()},
              "fine_float_max_absolute_rounding_usdc_per_eth": str(rounding),
              "separate_legs_vs_last_converted_tick": construction,
              "common_decisions": common_decisions, "common_markouts": common, "comparisons": comparisons,
              "fixed_examples": examples}
    write_table(root / "data/validation/reference_resolution/common_markouts.parquet", common["decision_rows"])
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports/reference_resolution.md").write_text(_render(report))
    return report


def _render(report: dict) -> str:
    lines = ["# Reference-resolution diagnosis", "", f"Status: **{report['status']}**. Fixed day: **2024-01-15 UTC**. Existing cache only; no network requests.", "",
             "A = saved Binance minute archives; B = independently closed ETHUSDT and USDCUSDT minutes, then divided; C = existing aggTrade reference.", "",
             "| Leg | Compared minutes | Exact closes | Price differences | Time errors | Missing trade / kline minutes |",
             "|---|---:|---:|---:|---:|---|" ]
    for symbol, audit in report["minute_audits"].items():
        lines.append(f"| {symbol} | {audit['compared_minutes']} | {audit['exact_price_matches']} | {audit['price_mismatch_count']} | {audit['time_mapping_error_count']} | {len(audit['missing_trade_minutes'])} / {len(audit['missing_kline_minutes'])} |")
    lines.extend(["", "Minutes are half-open UTC intervals `[u,u+60s)`. Validated chronological trades use `(raw millisecond timestamp, aggregate ID)` order; the last row wins, including timestamp ties. No empty minute is filled. Prices are compared as exact Decimals, with no tolerance.",
                  "Trade time and candle completion are separate. Raw kline close labels are `u+59.999s`; observation time is `u+60s` and assumed availability is `u+61s`. B uses the same candle observation age as A, preserving actual last-trade times separately.", "",
                  f"Saved A versus independently converted B: {len(report['saved_A_vs_reconstructed_B_differences'])} price/time mismatches; {len(report['minute_reference_missing_from_A'])} B minutes absent from A.", "",
                  "| Pair | Feature | Common decisions | Median / p95 absolute difference (bps) | Sign agreement |",
                  "|---|---|---:|---|---|" ])
    for pair, metrics in report["comparisons"].items():
        for metric, value in metrics.items():
            lines.append(f"| {pair} | {metric} | {value['comparable_count']} | {_number(value['median_absolute_difference_bps'])} / {_number(value['p95_absolute_difference_bps'])} | {value['sign_agreement_count']}/{value['comparable_count']} |")
    lines.extend(["", "Sign agreement for absolute deviations is mechanically nonnegative and supplies no directional validation."])
    counts = report["common_markouts"]["totals"]
    exclusions = {metric: len(mask["excluded"]) for metric, mask in report["common_decisions"].items()}
    lines.extend(["", f"From 24 fixed decisions, exclusions on the three-way common masks: {exclusions}. No numerical comparison tolerance was introduced.", "",
                  f"Mature Swap membership identical: **{report['common_markouts']['membership_identical']}**. A/B/C valid: {counts['A_valid_swaps']}/{counts['B_valid_swaps']}/{counts['C_valid_swaps']}; common: {counts['common_valid_swaps']}. Markouts are recomputed with the production formula on this three-way intersection using identical original `abs(amount0)/1e6` weights. Per-decision membership, exclusions and exact common volumes are saved.", "",
                  "| Variant | Mature Swaps | Immature Swaps | Missing / invalid endpoints |", "|---|---:|---:|---:|",
                  *[f"| {label} | {value['markout_mature_count']} | {value['markout_immature_count']} | {value['markout_missing_reference_count']} |" for label, value in report["variant_counts"].items()], "",
                  "## Fixed observations", "", "Each row shows ETHUSDT / USDCUSDT. A and B select the same candle; actual last trades shown for B also explain A's matching closes.", "",
                  "| Decision UTC | Variant | Leg prices | Observation times UTC (ETH / USDC) | Available UTC (ETH / USDC) | Leg ages (s) | Actual last trades UTC |",
                  "|---|---|---|---|---|---|---|" ])
    for example in report["fixed_examples"]:
        for label, trace in example["decision_references"].items():
            e, u = trace["legs"]["eth"], trace["legs"]["usdc"]
            short = lambda value: value[11:].replace("+00:00", "")
            lines.append(f"| {example['decision_time'][11:16]} | {label} | {e['price_usdt']} / {u['price_usdt']} | {short(e['observation_time'])} / {short(u['observation_time'])} | {short(e['available_at'])} / {short(u['available_at'])} | {e['age_seconds']:.3f} / {u['age_seconds']:.3f} | {short(e['actual_last_trade_time'])} / {short(u['actual_last_trade_time'])} |")
    lines.extend(["", "At hourly decisions A/B cannot use the candle completing at the decision because its +1s availability is still in the future; their selected completion is 60s old. C selects an ETH trade at least 1s old and a USDC trade observed no later than that ETH trade and available by the ETH quote's availability. Historical markout endpoints instead allow endpoint quotes received by the later decision, while keeping observation time at or before each fixed endpoint.", "",
                  "For each decision below, endpoints belong to the same first mature Swap across A/B/C. Each endpoint lists the fixed target time and selected reference price (USDC/ETH); the 00:00 example falls on Jan 14.", "",
                  "| Decision UTC | Variant | Initial target UTC / price | Five-minute target UTC / price | Initial leg ages (ETH / USDC, s) | Target leg ages (s) |",
                  "|---|---|---|---|---|---|"])
    for example in report["fixed_examples"]:
        for label, endpoints in example.get("swap", {}).get("endpoints", {}).items():
            initial, target = endpoints["initial"], endpoints["target"]
            ages = lambda trace: " / ".join(f"{trace['legs'][leg]['age_seconds']:.3f}" for leg in ("eth", "usdc"))
            lines.append(f"| {example['decision_time'][11:16]} | {label} | {initial['target_time']} / {initial['price_usdc_per_eth']} | {target['target_time']} / {target['price_usdc_per_eth']} | {ages(initial)} | {ages(target)} |")
    lines.extend(["",
                  "The observations change both ETH and USDC prices. The identity `P_C/P_B = (ETH_C/ETH_B)/(USDC_C/USDC_B)` explains their arithmetic conversion: at 12:00 ETHUSDT rises from 2539.21 to 2539.36, but USDCUSDT rises from 1.0005 to 1.0006, so the converted reference falls. This is evidence of conversion influence, not an independent economic causal decomposition."])
    changed = [row for row in report["separate_legs_vs_last_converted_tick"] if Decimal(row["difference_bps"]) != 0]
    lines.extend(["", f"Within the original comparison window, {len(report['separate_legs_vs_last_converted_tick'])} minutes end with USDC after ETH; {len(changed)} changes the final converted price. The saved witnesses show why closing already-converted ETH ticks would be a different construction.",
                  *[f"At {row['minute_open']}, ETH closes at {row['eth_trade_time']}; USDC changes from {row['usdc_at_eth_price']} to {row['usdc_final_price']} at {row['usdc_final_trade_time']}. Last-converted-tick versus separately closed legs: {Decimal(row['difference_bps']):.9f} bps." for row in changed], "",
                  f"C stores ratios as floats; the largest observed absolute Decimal-versus-stored ratio residual is {report['fine_float_max_absolute_rounding_usdc_per_eth']} USDC/ETH. This residual is retained, without a new tolerance. Exact A/B close differences are counted above.", "",
                  "## Finding and limits", "",
                  ("No implementation error was found in the checked minute construction, conversion or endpoint assignment. Under the same definition A and B agree exactly. Differences to C remain documented measurement sensitivity to aggregation and asynchronous observation selection."
                   if report["minutes_agree"] else
                   "The audit detected differences or coverage gaps; inspect the comparisons before treating A and B as equivalent. Production inputs have not been changed."), "",
                  "The cache supports the displayed price/time arithmetic, but not a unique split between market movement, aggregation and currency effects, or actual historical reception latency. Trade prices are not midprices; the finer variant is not treated as ground truth for toxicity.",
                  "The five-minute markout remains signed reference-price movement, without DEX execution price or fees. No windows, delays or thresholds were tuned. Production references, fees, execution, costs and primary targets were not changed.", "",
                  "The figure's common-sample values, Swap identities and exact weights are in `data/validation/reference_resolution/common_markouts.parquet`. Other comparisons are recomputed from the cache.", ""])
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/pilot.toml")
    args = parser.parse_args()
    result = diagnose_references(load_config(args.config))
    print(json.dumps({"status": result["status"], "network_requests": result["network_requests"],
                      "report": "reports/reference_resolution.md"}))
    raise SystemExit(2 if result["status"] == "FAIL" else 0)

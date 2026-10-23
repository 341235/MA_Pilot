"""Cached, fixed-day comparison of minute and aggregate-trade references."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from math import isclose
from pathlib import Path
import json

from .config import data_path
from .data import DataContractError
from .features import build_features, historical_markout
from .fine_reference import convert_aggtrades, load_aggtrades
from .storage import read_table, sha256
from .temporal import apply_availability, asof_index, backward_asof, reference_asof
from .uniswap import price_from_sqrt

METRICS = ("dex_cex_deviation_bps", "absolute_dex_cex_deviation_bps", "historical_markout_5m_bps")


def _utc(stamp: float) -> str:
    return datetime.fromtimestamp(float(stamp), timezone.utc).isoformat()


def _number(value) -> str:
    return "—" if value is None else f"{float(value):.6g}"


def _root_path(cfg: dict, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(cfg["_root"]) / path


def _rows(path: Path) -> list[dict]:
    rows = read_table(path)
    for row in rows:
        for key in ("flags", "reference_flags"):
            if isinstance(row.get(key), str):
                row[key] = json.loads(row[key])
    return rows


def _load_run(cfg: dict) -> tuple[dict, dict]:
    directory = data_path(cfg, "output_dir") / "real"
    path = directory / "manifest.json"
    manifest = json.loads(path.read_text())
    if manifest.get("mode") != "real" or manifest.get("scope", "full") != "full":
        raise DataContractError("Feature validation requires the existing full real run")
    parameters = manifest.get("query_parameters", {})
    if any(parameters.get(key) != cfg[key] for key in ("pool", "pilot", "timing")):
        raise DataContractError("Feature validation configuration differs from the recorded real run parameters")
    tables = {}
    for name in ("events", "pool_observations", "reference_prices", "features", "outcomes"):
        artifact = directory / f"{name}.parquet"
        if manifest["artifacts"].get(artifact.name) != sha256(artifact):
            raise DataContractError("Feature validation input differs from its recorded run artifact")
        tables[name] = _rows(artifact)
    if not tables["pool_observations"]:
        raise DataContractError("Prepare and run confirmed pool observations before feature validation")
    return tables, manifest


def _fine_quotes(cfg: dict, day: date, start: float, end: float) -> tuple[list[dict], dict]:
    directory = _root_path(cfg, cfg["data"].get("fine_reference_dir", "data/cache/binance/aggTrades"))
    pairs = {"ETHUSDT": [], "USDCUSDT": []}
    for symbol in pairs:
        for archive_day in (day - timedelta(days=1), day):
            path = directory / f"{symbol}-aggTrades-{archive_day.isoformat()}.zip"
            sidecar, manifest_path = Path(str(path) + ".CHECKSUM"), Path(str(path) + ".manifest.json")
            if not all(value.is_file() for value in (path, sidecar, manifest_path)):
                raise DataContractError(f"Missing cached fine reference evidence: {path.name}")
            manifest = json.loads(manifest_path.read_text())
            checksum = sidecar.read_text().split()
            digest = sha256(path)
            if (len(checksum) != 2 or checksum[0].lower() != digest
                    or checksum[1].lstrip("*") != path.name
                    or manifest.get("sha256") != digest or manifest.get("checksum_verified") is not True
                    or manifest.get("symbol") != symbol or manifest.get("day") != archive_day.isoformat()
                    or not manifest.get("retrieved_at")):
                raise DataContractError(f"Fine reference provenance/checksum mismatch: {path.name}")
            selected = load_aggtrades(path, symbol, archive_day, start=start, end=end)
            pairs[symbol].extend(selected)
    quotes, diagnostics = convert_aggtrades(pairs["ETHUSDT"], pairs["USDCUSDT"],
                                           delay_seconds=cfg["timing"]["reference_delay_seconds"],
                                           max_age_seconds=cfg["timing"]["max_price_age_seconds"])
    return quotes, diagnostics


def proxy_measurements(decisions: list[float], events: list[dict], references,
                       observations: list[dict], cfg: dict, *, block_delay_seconds: float) -> list[dict]:
    """Use the production joins and markout formula without full-tick volatility."""
    timing = cfg["timing"]
    refs = asof_index(references)
    pool = asof_index(apply_availability(observations, block_delay_seconds))
    swaps = apply_availability(events, block_delay_seconds)
    result = []
    for decision in decisions:
        quote = reference_asof(refs, decision, timing["max_price_age_seconds"])
        state = backward_asof(pool, decision, timing["max_pool_age_seconds"])
        pool_row, reference = state.get("row"), quote.get("row")
        price = float(price_from_sqrt(int(pool_row["sqrt_price_x96"]))) if pool_row else None
        ref_price = float(quote["value"]) if quote["value"] is not None else None
        deviation = 10_000 * (price / ref_price - 1) if price is not None and ref_price and ref_price > 0 else None
        markout = historical_markout(swaps, refs, decision,
                                    lookback_seconds=timing["lookback_seconds"],
                                    horizon_seconds=timing["markout_seconds"],
                                    max_price_age_seconds=timing["max_price_age_seconds"],
                                    reference_delay_seconds=timing["reference_delay_seconds"])
        row = {"decision_time": decision,
               "pool_price_usdc_per_eth": price, "reference_price_usdc_per_eth": ref_price,
               "pool_observed_at": pool_row["timestamp"] if pool_row else None,
               "pool_state_changed_at": pool_row.get("state_changed_at") if pool_row else None,
               "pool_age_seconds": state["age_seconds"],
               "reference_timestamp": reference["timestamp"] if reference else None,
               "eth_age_seconds": quote.get("eth_age_seconds"), "usdc_age_seconds": quote.get("usdc_age_seconds"),
               "reference_flags": quote["flags"], "pool_flags": state["flags"],
               "dex_cex_deviation_bps": deviation,
               "absolute_dex_cex_deviation_bps": abs(deviation) if deviation is not None else None,
               **markout}
        result.append(row)
    return result


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


def compare_measurements(left: list[dict], right: list[dict], metric: str) -> dict:
    """Compare matching decisions; null observations never become zero."""
    by_left, by_right = ({row["decision_time"]: row for row in rows} for rows in (left, right))
    pairs, excluded = [], Counter()
    for decision in sorted(by_left.keys() | by_right.keys()):
        a, b = by_left.get(decision), by_right.get(decision)
        av, bv = a.get(metric) if a else None, b.get(metric) if b else None
        if av is None or bv is None:
            category = "both_missing" if av is None and bv is None else "left_missing" if av is None else "right_missing"
            excluded[category] += 1
            for label, row, value in (("left", a, av), ("right", b, bv)):
                if value is None:
                    reasons = ([*row.get("reference_flags", []), *row.get("pool_flags", [])]
                               if row and metric != "historical_markout_5m_bps" else row.get("markout_flags", []) if row else [])
                    for flag in reasons or ["MISSING_MEASUREMENT"]:
                        excluded[f"{label}:{flag}"] += 1
            continue
        pairs.append({"decision_time": decision, "left": av, "right": bv,
                      "absolute_difference_bps": abs(bv - av), "sign_agrees": _sign(av) == _sign(bv)})
    differences = [row["absolute_difference_bps"] for row in pairs]
    return {"metric": metric, "candidate_decisions": len(by_left.keys() | by_right.keys()),
            "comparable_count": len(pairs), "excluded_count": len(by_left.keys() | by_right.keys()) - len(pairs),
            "exclusion_reasons": dict(excluded), "median_absolute_difference_bps": _quantile(differences, .5),
            "p95_absolute_difference_bps": _quantile(differences, .95),
            "sign_agreement_count": sum(row["sign_agrees"] for row in pairs),
            "sign_agreement_fraction": sum(row["sign_agrees"] for row in pairs) / len(pairs) if pairs else None,
            "sign_convention": "negative, zero and positive are distinct; no rounding tolerance",
            "quantile_convention": "linear interpolation at (n-1)*p", "pairs": pairs}


def _verify_minute(measurements: list[dict], decisions: list[float], events: list[dict],
                   references: list[dict], observations: list[dict], cfg: dict) -> dict:
    production = build_features(decisions, events, references, observations,
                                {**cfg, "_onchain_coverage_complete": True})
    mismatches = []
    for expected, actual in zip(production, measurements):
        for metric in METRICS:
            a, b = expected[metric], actual[metric]
            if (a is None) != (b is None) or (a is not None and not isclose(a, b, rel_tol=1e-12, abs_tol=1e-9)):
                mismatches.append({"decision_time": actual["decision_time"], "metric": metric, "expected": a, "actual": b})
    return {"status": "FAIL" if mismatches else "PASS", "comparisons": len(measurements) * len(METRICS),
            "absolute_tolerance": 1e-9, "relative_tolerance": 1e-12, "mismatches": mismatches}


def common_trade_markouts(decisions: list[float], events: list[dict], left_references,
                          right_references, cfg: dict, *, block_delay_seconds: float) -> dict:
    """Keep identical trades and quote-volume weights when endpoint coverage differs."""
    timing = cfg["timing"]
    swaps = apply_availability(events, block_delay_seconds)
    references = [asof_index(left_references), asof_index(right_references)]
    rows = []
    totals = Counter()
    for decision in decisions:
        eligible = [event for event in swaps if event["event_type"].lower() == "swap"
                    and decision - timing["lookback_seconds"] < float(event["timestamp"]) <= decision
                    and float(event["available_at"]) <= decision
                    and float(event["timestamp"]) + timing["markout_seconds"] + timing["reference_delay_seconds"] <= decision
                    and int(event["amount0"]) != 0 and int(event["amount1"]) != 0]
        valid = [{}, {}]
        weights = {}
        for event in eligible:
            key = f"{event['transaction_hash']}:{event.get('log_index', 0)}"
            if key in weights:
                raise DataContractError("Duplicate Swap identity in markout comparison")
            weights[key] = Decimal(abs(int(event["amount0"]))) / Decimal(10**6)
            for index, reference in enumerate(references):
                result = historical_markout([event], reference, decision,
                                             lookback_seconds=timing["lookback_seconds"],
                                             horizon_seconds=timing["markout_seconds"],
                                             max_price_age_seconds=timing["max_price_age_seconds"],
                                             reference_delay_seconds=timing["reference_delay_seconds"])
                if result["historical_markout_5m_bps"] is not None:
                    valid[index][key] = result["historical_markout_5m_bps"]
        left_keys, right_keys = set(valid[0]), set(valid[1])
        common = left_keys & right_keys
        denominator = sum((weights[key] for key in common), Decimal(0))
        values = [float(sum((weights[key] * Decimal(str(side[key])) for key in common), Decimal(0)) / denominator)
                  if denominator else None for side in valid]
        counts = {"candidate_mature_swaps": len(eligible), "left_valid_swaps": len(left_keys),
                  "right_valid_swaps": len(right_keys), "common_valid_swaps": len(common),
                  "left_only_swaps": len(left_keys - right_keys), "right_only_swaps": len(right_keys - left_keys),
                  "both_invalid_swaps": len(eligible) - len(left_keys | right_keys)}
        totals.update(counts)
        rows.append({"decision_time": decision, **counts, "common_quote_volume_usdc": float(denominator),
                     "left_common_markout_bps": values[0], "right_common_markout_bps": values[1],
                     "left_only_identities": sorted(left_keys - right_keys),
                     "right_only_identities": sorted(right_keys - left_keys)})
    comparison = compare_measurements(
        [{"decision_time": row["decision_time"], "historical_markout_5m_bps": row["left_common_markout_bps"]} for row in rows],
        [{"decision_time": row["decision_time"], "historical_markout_5m_bps": row["right_common_markout_bps"]} for row in rows],
        "historical_markout_5m_bps")
    return {"membership_identical": totals["left_only_swaps"] == totals["right_only_swaps"] == 0,
            "totals": dict(totals), "decision_rows": rows, "comparison": comparison,
            "analytical_recombination_tolerance_bps": 1e-9,
            "weighting": "Same original abs(amount0)/1e6 USDC weights on the intersection; per-trade bps use the production historical_markout function"}


def _render(report: dict) -> str:
    selection, pilot = report["selection"], report["real_run"]
    conversion = report.get("fine_conversion_diagnostics", {})
    lines = ["# Feature measurement checks", "", f"Status: **{report['status']}**. Measurements under stated availability assumptions; no predictive or profitability claim.", "",
             f"Fixed comparison day: **{selection['day']} UTC**, all 24 hourly decisions. History starts {selection['reference_start_utc']}; selected reference data end {selection['reference_end_utc']} (exclusive).",
             f"Only block reception changes in the delay comparison: {selection['baseline_block_delay_seconds']}s versus {selection['alternate_block_delay_seconds']}s. Reference delay stays {selection['reference_delay_seconds']}s; both maximum ages stay 120s.", "",
             "## Temporal correction and full pilot", "",
             f"The saved real run has {pilot['feature_rows']} feature rows, {pilot['valid_feature_rows']} satisfying required pool/reference joins, {pilot['scenario_rows']} scenarios and {pilot['sensitivity_y_nonmissing']} sensitivity targets. Primary Y: {pilot['primary_y_nonmissing']}. Overall pilot status: **{pilot['status']}**.",
             "Block observation time, last state change and assumed availability are separate. Original events, integer replay and fee calculations remain unchanged.", "",
             "| Full-pilot feature | Available decisions |", "|---|---:|",
             *[f"| {name} | {count}/{pilot['feature_rows']} |" for name, count in pilot.get("feature_nonmissing_counts", {}).items()], "",
             "## Minute versus aggregate trades", "",
             f"Fine-data status: **{report['fine_status']}**. {report.get('fine_error', '')}",
             f"Minute measurements agree with the production feature builder: {report['minute_formula_regression']['status']} ({report['minute_formula_regression']['comparisons']} comparisons).", "",
             (f"Fine quote conversion: {conversion['converted_count']}/{conversion['eth_trade_count']} selected ETH trades; {conversion['missing_usdc_count']} lack a prior USDC quote, {conversion['stale_usdc_count']} have a stale conversion and {conversion['unavailable_usdc_count']} have an unavailable conversion. {conversion['asynchronous_leg_count']} valid quotes have different leg timestamps. These quote exclusions are separate from the decision-level exclusions below."
              if conversion else "Fine quote conversion is unavailable."), "",
             "| Block delay | Metric | Common decisions | Excluded | Median absolute difference (bps) | 95% absolute difference (bps) | Sign agreement |", "|---|---|---:|---:|---:|---:|---:|"]
    for label, metrics in report["granularity_comparisons"].items():
        for metric, comparison in metrics.items():
            lines.append(f"| {label} | {metric} | {comparison['comparable_count']} | {comparison['excluded_count']} | {_number(comparison['median_absolute_difference_bps'])} | {_number(comparison['p95_absolute_difference_bps'])} | {comparison['sign_agreement_count']}/{comparison['comparable_count']} |")
    lines.extend(["", "Differences are measured on decisions where both values exist. Zero is a distinct sign; quantiles use linear interpolation.",
                  "Each historical markout averages its valid mature swaps. Quote gaps can change trade membership even when both decision aggregates exist; per-variant counters below disclose that difference.", "", "| Variant | Valid deviation | Valid historical markout | Mature swaps across windows | Immature swaps | Missing/invalid endpoints |", "|---|---:|---:|---:|---:|---:|"])
    for label, counts in report["variant_counts"].items():
        lines.append(f"| {label} | {counts['deviation_nonmissing']} | {counts['markout_nonmissing']} | {counts['markout_mature_count']} | {counts['markout_immature_count']} | {counts['markout_missing_reference_count']} |")
    for label, result in report.get("common_trade_markouts", {}).items():
        counts, comparison = result["totals"], result["comparison"]
        lines.extend(["", f"Common-trade check ({label}): identical valid membership **{result['membership_identical']}**; {counts['common_valid_swaps']} common mature Swaps, {counts['left_only_swaps']} minute-only and {counts['right_only_swaps']} fine-only. Reweighted common-trade median / 95% absolute differences: {_number(comparison['median_absolute_difference_bps'])} / {_number(comparison['p95_absolute_difference_bps'])} bps; sign agreement {comparison['sign_agreement_count']}/{comparison['comparable_count']} decisions."])
    lines.extend(["", "Common-trade recombination uses original quote-volume weights and the production per-trade markout; its numerical comparison tolerance is 1e-9 bps."])
    lines.extend(["", "Sign agreement for absolute deviations is mechanically nonnegative; the signed deviation and markout rows carry directional information.", "",
                  "## Delay sensitivity", "", "| Reference | Metric | Common decisions | Median / 95% absolute difference (bps) | Sign agreement |", "|---|---|---:|---|---|"])
    for label, metrics in report["delay_comparisons"].items():
        for metric, comparison in metrics.items():
            lines.append(f"| {label} | {metric} | {comparison['comparable_count']} | {_number(comparison['median_absolute_difference_bps'])} / {_number(comparison['p95_absolute_difference_bps'])} | {comparison['sign_agreement_count']}/{comparison['comparable_count']} |")
    lines.extend(["", "## Fixed examples", "", "Examples were fixed at 00:00, 12:00 and 23:00 UTC. Currency-leg prices and times are examined in [reference_resolution.md](reference_resolution.md).", "", "| UTC decision | Minute / fine deviation (bps) | Minute / fine markout (bps) | Pool observed / last changed |", "|---|---|---|---|"])
    for example in report["fixed_decision_examples"]:
        minute, fine = example["minute"], example.get("fine") or {}
        observed = _utc(minute["pool_observed_at"]) if minute["pool_observed_at"] is not None else "—"
        changed = _utc(minute["pool_state_changed_at"]) if minute["pool_state_changed_at"] is not None else "unknown"
        lines.append(f"| {_utc(example['decision_time'])} | {_number(minute['dex_cex_deviation_bps'])} / {_number(fine.get('dex_cex_deviation_bps'))} | {_number(minute['historical_markout_5m_bps'])} / {_number(fine.get('historical_markout_5m_bps'))} | {observed} / {changed} |")
    lines.extend(["", "Endpoints stay anchored to trade time and trade time +300s; a later price before the decision cannot replace them.", "", "## Evidence and limits", "",
                  "A Swap at 12:00:20 targets the reference price at 12:05:20. With a one-second reference delay, its historical markout cannot enter a decision before 12:05:21, and both required quotes must actually have arrived. A 12:05:40 price cannot replace that target.",
                  "Historical aggregate trade prices are not order-book midprices. Both currency legs are observed; asynchronous legs are flagged and stale conversion quotes excluded. Retrieval times establish provenance, not historical information arrival.",
                  "The sampled gas warning remains in the full feature output. Position fees, operation-cost calibration and primary targets remain open. Agreement between references establishes neither true toxicity nor forecasting value.",
                  "Definitions: [feature_definitions.md](../docs/feature_definitions.md). The comparison is reproducible from the cached minute and aggTrade archives.", ""])
    example = report.get("worked_pool_example")
    if example:
        lines.extend(["## Pool-time example", "",
                      f"At {_utc(example['decision_time'])}, the relevant state last changed at {_utc(example['changed_at'])}. Header block {example['observed_block']} confirms it through {_utc(example['observed_at'])}; assumed availability is {_utc(example['available_at'])}. The observation is {example['age_seconds']}s old, although the last change is {example['change_age_seconds']}s old. The 120s observation limit is unchanged.", ""])
    return "\n".join(line.rstrip() for line in lines)


def validate_features(cfg: dict) -> dict:
    """Run the predetermined measurement study using saved inputs only."""
    specification = cfg.get("feature_validation", {})
    day = date.fromisoformat(specification.get("day", "2024-01-15"))
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).timestamp()
    decisions = [start + 3600 * index for index in range(24)]
    history_start, reference_end = start - 3900, decisions[-1] + 60
    timing = cfg["timing"]
    base, alternate = timing["block_delay_seconds"], specification.get("alternate_block_delay_seconds", 60)
    tables, manifest = _load_run(cfg)
    minute_refs = asof_index(apply_availability(tables["reference_prices"], timing["reference_delay_seconds"]))
    variants = {}
    for label, delay in (("baseline", base), ("delay_60s", alternate)):
        variants[f"minute_{label}"] = proxy_measurements(decisions, tables["events"], minute_refs,
                                                        tables["pool_observations"], cfg, block_delay_seconds=delay)
    regression = _verify_minute(variants["minute_baseline"], decisions, tables["events"], tables["reference_prices"], tables["pool_observations"], cfg)
    fine_status, fine_error, conversion, common_trades = "BLOCKED", None, {}, {}
    try:
        fine, conversion = _fine_quotes(cfg, day, history_start, reference_end)
        fine_refs = asof_index(fine)
        del fine
        for label, delay in (("baseline", base), ("delay_60s", alternate)):
            variants[f"fine_{label}"] = proxy_measurements(decisions, tables["events"], fine_refs,
                                                          tables["pool_observations"], cfg, block_delay_seconds=delay)
            common_trades[label] = common_trade_markouts(decisions, tables["events"], minute_refs,
                                                        fine_refs, cfg, block_delay_seconds=delay)
        fine_status = "WARN"
    except (ValueError, OSError, KeyError) as exc:
        fine_error = str(exc)
    granularity = {label: {metric: compare_measurements(variants[f"minute_{label}"], variants[f"fine_{label}"], metric) for metric in METRICS}
                   for label in ("baseline", "delay_60s") if f"fine_{label}" in variants}
    delay_comparisons = {label: {metric: compare_measurements(variants[f"{label}_baseline"], variants[f"{label}_delay_60s"], metric) for metric in METRICS}
                         for label in ("minute", "fine") if f"{label}_baseline" in variants}
    counts = {label: {"deviation_nonmissing": sum(row["dex_cex_deviation_bps"] is not None for row in rows),
                      "markout_nonmissing": sum(row["historical_markout_5m_bps"] is not None for row in rows),
                      **{key: sum(int(row[key]) for row in rows) for key in ("markout_mature_count", "markout_immature_count", "markout_missing_reference_count")}}
              for label, rows in variants.items()}
    full = tables["features"]
    report = {"status": "FAIL" if regression["status"] == "FAIL" else "WARN", "fine_status": fine_status,
              "network_requests": 0,
              "selection": {"day": day.isoformat(), "decision_times": decisions,
                            "reference_start_utc": _utc(history_start), "reference_end_utc": _utc(reference_end),
                            "baseline_block_delay_seconds": base, "alternate_block_delay_seconds": alternate,
                            "reference_delay_seconds": timing["reference_delay_seconds"],
                            "maximum_reference_age_seconds": timing["max_price_age_seconds"],
                            "maximum_pool_age_seconds": timing["max_pool_age_seconds"]},
              "real_run": {"status": manifest["status"], "feature_rows": len(full),
                           "valid_feature_rows": sum(row["status"] != "BLOCKED" for row in full),
                           "scenario_rows": len(tables["outcomes"]),
                           "primary_y_nonmissing": sum(row["y_usdc"] is not None for row in tables["outcomes"]),
                           "sensitivity_y_nonmissing": sum(row["sensitivity_y_usdc"] is not None for row in tables["outcomes"]),
                           "feature_nonmissing_counts": manifest["diagnostics"].get("feature_nonmissing_counts", {})},
              "minute_formula_regression": regression, "variant_counts": counts,
              "fine_conversion_diagnostics": conversion,
              "granularity_comparisons": granularity, "delay_comparisons": delay_comparisons,
              "common_trade_markouts": common_trades,
              "fixed_decision_examples": [{"decision_time": decision,
                                            "minute": variants["minute_baseline"][index],
                                            "fine": variants.get("fine_baseline", [None] * 24)[index]}
                                           for index, decision in enumerate(decisions) if index in {0, 12, 23}]}
    if fine_error:
        report["fine_error"] = fine_error
    worked_row = next((row for row in full if float(row["decision_time"]) == decisions[7]), None)
    if worked_row and all(worked_row.get(key) is not None for key in ("pool_observed_at", "pool_state_changed_at", "pool_available_at")):
        report["worked_pool_example"] = {"decision_time": worked_row["decision_time"],
                                         "changed_at": worked_row["pool_state_changed_at"],
                                         "observed_at": worked_row["pool_observed_at"],
                                         "available_at": worked_row["pool_available_at"],
                                         "observed_block": worked_row["pool_block_number"],
                                         "age_seconds": worked_row["pool_age_seconds"],
                                         "change_age_seconds": float(worked_row["decision_time"]) - float(worked_row["pool_state_changed_at"])}
    reports = _root_path(cfg, "reports")
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "feature_validation.md").write_text(_render(report))
    return report

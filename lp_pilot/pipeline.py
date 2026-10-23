"""Load inputs, run the pilot and write results."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from statistics import median

from .config import data_path, decisions, required_interval
from .storage import sha256, write_json, write_table
from .validation import Check, final_status, serialize


def load_references(cfg: dict) -> tuple[list[dict], list[dict], list[str]]:
    """Read both reference pairs and convert ETH/USDT to ETH/USDC."""
    from .data import DataContractError, load_binance_klines
    directory = data_path(cfg, "reference_dir")
    pairs: dict[str, dict[float, dict]] = {"ETHUSDT": {}, "USDCUSDT": {}}
    sources, errors = [], []
    for symbol in pairs:
        for path in sorted(directory.glob(f"{symbol}-1m-*.zip")):
            try:
                sidecar = path.with_name(path.name + ".CHECKSUM")
                if not sidecar.exists() or sha256(path) != sidecar.read_text().split()[0].lower():
                    raise DataContractError("Missing or mismatching checksum sidecar")
                rows = load_binance_klines(path, symbol)
                for row in rows:
                    stamp = row["timestamp"]
                    if stamp in pairs[symbol]:
                        raise DataContractError("Overlapping reference archives")
                    pairs[symbol][stamp] = row
                sources.append({"source": "Binance Public Data", "symbol": symbol,
                                "path": str(path.relative_to(cfg["_root"])) if path.is_relative_to(cfg["_root"]) else str(path),
                                "sha256": sha256(path), "row_count": len(rows), "checksum_verified": True})
            except (ValueError, OSError, IndexError) as exc:
                errors.append(f"{path.name}: {type(exc).__name__}")
    start, end = required_interval(cfg)
    references = []
    for stamp in sorted(pairs["ETHUSDT"].keys() & pairs["USDCUSDT"].keys()):
        if start <= stamp < end:
            eth, usdc = pairs["ETHUSDT"][stamp], pairs["USDCUSDT"][stamp]
            conversion = Decimal(usdc["close"])
            if conversion <= 0:
                errors.append("Nonpositive USDCUSDT reference")
                continue
            references.append({"timestamp": stamp,
                               "available_at": stamp + cfg["timing"]["reference_delay_seconds"],
                               "price_usdc_per_eth": Decimal(eth["close"]) / conversion,
                               "ethusdt": eth["close"], "usdcusdt": usdc["close"],
                               "eth_timestamp": stamp, "usdc_timestamp": stamp,
                               "eth_available_at": stamp + cfg["timing"]["reference_delay_seconds"],
                               "usdc_available_at": stamp + cfg["timing"]["reference_delay_seconds"],
                               "reference_price_kind": "closed_minute_candle",
                               "reference_granularity_seconds": 60,
                               "source": "Binance closed 1m ETHUSDT / USDCUSDT"})
    return references, sources, errors


def fetch_references(cfg: dict) -> dict:
    from .data import RequestBudget, download_binance_daily
    start, end = required_interval(cfg)
    day = datetime.fromtimestamp(start, timezone.utc).date()
    final_day = datetime.fromtimestamp(end - 1, timezone.utc).date()
    budget = RequestBudget(cfg["network"]["max_requests"], cfg["network"]["max_download_bytes"])
    sources = []
    from datetime import timedelta
    while day <= final_day:
        for symbol in ("ETHUSDT", "USDCUSDT"):
            sources.append(download_binance_daily(symbol, day, data_path(cfg, "reference_dir"),
                                                  budget=budget, timeout=cfg["network"]["timeout_seconds"]))
        day += timedelta(days=1)
    return {"sources": sources, "requests": budget.used_requests, "bytes": budget.used_bytes}


def _checkpoint_checks(snapshot: dict, states: list[dict], cfg: dict) -> Check:
    from .data import DataContractError, parse_integer
    from .rpc_ingest import checked_artifact, historical_state
    path = data_path(cfg, "checkpoints_path")
    if not path.exists():
        return Check("reconstruction", "BLOCKED", "No independent historical checkpoint file")
    payload = json.loads(path.read_text())
    checkpoints = payload.get("checkpoints", []) if isinstance(payload, dict) else payload
    if not checkpoints:
        return Check("reconstruction", "BLOCKED", "Independent checkpoint list is empty")
    comparisons = []
    cfg["_checkpoint_comparisons"] = comparisons
    for checkpoint in checkpoints:
        if checkpoint.get("source_kind") != "historical_rpc":
            return Check("reconstruction", "BLOCKED", "Checkpoint requires separately saved historical_rpc evidence")
        try:
            evidence = checked_artifact(checkpoint, cfg["_root"])
            actual = historical_state(evidence, pool_address=cfg["pool"]["address"])
            number = parse_integer(checkpoint["block_number"])
            if actual["block_number"] != number:
                raise DataContractError("Checkpoint index and RPC evidence block numbers differ")
            if "block_hash" in checkpoint and checkpoint["block_hash"].lower() != actual["block_hash"]:
                raise DataContractError("Checkpoint index and RPC evidence block hashes differ")
            extraction = cfg.get("_rpc_export_manifest")
            if extraction and number >= int(extraction["block_end_exclusive"]):
                raise DataContractError("Checkpoint lies beyond verified log coverage")
            eligible = [row for row in [snapshot, *states] if int(row["block_number"]) <= number]
            if not eligible:
                return Check("reconstruction", "FAIL", "Checkpoint predates initialization")
            reconstructed = max(eligible, key=lambda row: (int(row["block_number"]), int(row.get("transaction_index", -1)), int(row.get("log_index", -1))))
            if int(reconstructed["block_number"]) == number and reconstructed["block_hash"].lower() != actual["block_hash"]:
                raise DataContractError("Reconstructed and checkpoint block hashes differ")
            for key in ("sqrt_price_x96", "tick", "liquidity", "fee_growth_global0_x128", "fee_growth_global1_x128"):
                # Decode the saved call rather than trusting the checkpoint summary.
                expected, observed = actual[key], reconstructed.get(key)
                difference = None if observed is None else parse_integer(observed) - expected
                comparisons.append({"block_number": number, "field": key, "rpc_value": str(expected),
                                    "reconstructed_value": None if observed is None else str(observed),
                                    "difference": None if difference is None else str(difference),
                                    "status": "BLOCKED" if difference is None else "PASS" if difference == 0 else "FAIL"})
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return Check("reconstruction", "BLOCKED", f"Historical RPC checkpoint evidence rejected: {exc}")
    mismatches = [row for row in comparisons if row["status"] == "FAIL"]
    if mismatches:
        return Check("reconstruction", "FAIL", "Historical RPC checkpoint differences; integer tolerance=0: " + json.dumps(mismatches, sort_keys=True))
    if any(row["status"] == "BLOCKED" for row in comparisons):
        return Check("reconstruction", "BLOCKED", "Replay lacks one or more historical checkpoint state/fee-growth fields")
    return Check("reconstruction", "PASS", f"{len(checkpoints)} separately called historical checkpoints: exact price, tick, active liquidity and both global fee-growth comparisons; same RPC provider, independent method from event replay; integer tolerance=0")


def run(cfg: dict, mode: str = "real") -> dict:
    """Run the cached pilot; synthetic input is used only by integration tests."""
    from .features import build_features
    from .outcomes import evaluate_episodes
    from .replay import replay_events
    from .synthetic import generate
    output = data_path(cfg, "output_dir") / mode
    output.mkdir(parents=True, exist_ok=True)
    checks: list[Check] = []
    cfg.pop("_checkpoint_comparisons", None)
    cfg.pop("_rpc_export_manifest", None)
    cfg.pop("_pool_observation_summary", None)
    onchain_complete = False
    snapshot, events, references, receipts, sources, states, segments = None, [], [], [], [], [], []
    pool_observations = None
    if mode == "synthetic":
        snapshot, events, references, receipts = generate(cfg)
        onchain_complete = True
        sources.append({"source": "deterministic synthetic fixture", "seed": cfg["pilot"]["synthetic_seed"],
                        "empirical": False, "generation": "lp_pilot.synthetic.generate"})
        checks.extend([
            Check("pool_identity", "WARN", "Configured pool constants; artificial data do not verify deployed contract"),
            Check("coverage", "PASS", "Artificial minute observations cover warmup and outcome/markout tail"),
            Check("initialization", "PASS", "Artificial nonzero full tick distribution before first processed block"),
        ])
    else:
        from .rpc_ingest import load_rpc_export, validate_rpc_snapshot
        source_manifest = None
        references, source_rows, reference_errors = load_references(cfg)
        sources.extend(source_rows)
        checks.append(Check("reference_import", "FAIL" if reference_errors else "PASS" if references else "BLOCKED",
                            "; ".join(reference_errors) if reference_errors else f"{len(references)} converted closed-minute reference observations; no USDC=USDT assumption"))
        for key, target in (("snapshot_path", "snapshot"), ("receipts_path", "receipts")):
            path = data_path(cfg, key)
            if path.exists():
                value = json.loads(path.read_text())
                sources.append({"source": target, "path": cfg["data"][key], "sha256": sha256(path)})
                if target == "snapshot":
                    snapshot = value
                else:
                    receipts = value.get("receipts", []) if isinstance(value, dict) else value
        if data_path(cfg, "events_path").exists() and data_path(cfg, "export_manifest_path").exists():
            try:
                first, last = required_interval(cfg)
                events, source_manifest = load_rpc_export(data_path(cfg, "events_path"), data_path(cfg, "export_manifest_path"),
                                                 pool_address=cfg["pool"]["address"], start=first, end=last, root=cfg["_root"])
                sources.append({"source": "rpc", "path": cfg["data"]["export_manifest_path"],
                                "sha256": sha256(data_path(cfg, "export_manifest_path"))})
                cfg["_rpc_export_manifest"] = source_manifest
                onchain_complete = True
                reference_stamps = {int(row["timestamp"]) for row in references}
                expected_minutes = set(range((first // 60 + 1) * 60, last, 60))
                complete = expected_minutes.issubset(reference_stamps)
                checks.append(Check("coverage", "PASS" if complete else "BLOCKED",
                                    f"Contiguous RPC block intervals, raw-log artifacts, counts, headers and checksum verified; reference coverage {'complete' if complete else 'incomplete'}"))
            except (ValueError, OSError) as exc:
                checks.append(Check("coverage", "FAIL", f"Ethereum RPC export rejected: {exc}"))
        else:
            checks.append(Check("coverage", "BLOCKED", "Missing provenance-checked Ethereum RPC raw-log artifacts: events_path and export_manifest_path"))
        probe_path = Path(cfg["_root"]) / "data/input/rpc/access_probe.json"
        probe = json.loads(probe_path.read_text()) if probe_path.exists() else {}
        identity = probe.get("rpc", {}).get("pool_identity", {})
        facts = identity.get("value", {})
        identity_matches = identity.get("status") == "PASS" and all(
            str(facts.get(key, "")).lower() == str(value).lower()
            for key, value in {"pool_address": cfg["pool"]["address"], "chain_id": cfg["pool"]["chain_id"],
                               "token0": cfg["pool"]["token0"], "token1": cfg["pool"]["token1"],
                               "decimals0": cfg["pool"]["decimals0"], "decimals1": cfg["pool"]["decimals1"],
                               "fee": cfg["pool"]["fee_pips"], "tickSpacing": cfg["pool"]["tick_spacing"]}.items())
        checks.append(Check("pool_identity", "PASS" if identity_matches else "BLOCKED",
                            "Saved RPC immutable and token-decimal calls match configured pool; historical access is separate" if identity_matches else
                            "Saved deployed-contract identity is missing or differs from configuration; fetch-rpc verifies it"))
        if probe_path.exists():
            sources.append({"source": "saved RPC access probe", "path": "data/input/rpc/access_probe.json", "sha256": sha256(probe_path)})
        if snapshot and source_manifest:
            try:
                expected_block = int(events[0]["block_number"] if events else source_manifest["block_start"]) - 1
                validate_rpc_snapshot(snapshot, data_path(cfg, "snapshot_path"),
                    root=cfg["_root"], pool_address=cfg["pool"]["address"], expected_block=expected_block,
                    tick_spacing=cfg["pool"]["tick_spacing"], fee_pips=cfg["pool"]["fee_pips"])
                snapshot_path = data_path(cfg, "snapshot_path")
                snapshot_index = snapshot_path.with_name(snapshot_path.name + ".manifest.json")
                sources.append({"source": "snapshot evidence index", "path": str(snapshot_index), "sha256": sha256(snapshot_index)})
                checks.append(Check("initialization", "PASS", f"Full historical bitmap/tick state independently decoded from hash-pinned RPC artifacts at block {expected_block}; snapshot checksum and liquidity invariants verified; same-provider evidence"))
            except (ValueError, OSError, KeyError, TypeError) as exc:
                checks.append(Check("initialization", "BLOCKED", f"Historical full snapshot evidence rejected: {exc}"))
        else:
            checks.append(Check("initialization", "BLOCKED",
                                "Require evidenced full tick state at first processed block minus one; warmup is insufficient"))
    initialization = next((check for check in checks if check.name == "initialization"), None)
    initialization_ready = bool(initialization and initialization.status == "PASS")
    if snapshot and (events or onchain_complete) and initialization_ready:
        try:
            replay = replay_events(snapshot, events)
            states, segments = replay.states, replay.segments
            for check in replay.checks:
                if mode == "real" and check.get("check") == "independent_historical_checkpoints":
                    continue  # The critical gate below validates saved RPC responses.
                checks.append(Check(f"replay_{check.get('check', check.get('name', 'check'))}", check["status"], check["detail"]))
            checks.append(Check("reconstruction", "PASS", "Integer replay is internally consistent; not independent evidence")
                          if mode == "synthetic" else _checkpoint_checks(snapshot, states, cfg))
        except ValueError as exc:
            checks.append(Check("reconstruction", "FAIL", f"Replay rejected: {exc}"))
    else:
        detail = ("Real replay requires evidenced historical initialization; no analytic states or fees are derived from synthetic or unevidenced snapshots"
                  if mode == "real" and not initialization_ready else "Replay requires events and initialized tick state")
        checks.append(Check("reconstruction", "BLOCKED", detail))
    if mode == "real" and states:
        header_path = data_path(cfg, "events_path").parent / "observation_headers.json"
        if header_path.exists():
            try:
                from .observation_headers import load_observation_headers
                from .pool_observations import confirm_pool_observations
                headers, header_summary = load_observation_headers(header_path, root=cfg["_root"],
                    export_manifest_path=data_path(cfg, "export_manifest_path"))
                pool_observations, summary = confirm_pool_observations(snapshot, events, states,
                    cfg["_rpc_export_manifest"], headers)
                cfg["_pool_observation_summary"] = {**header_summary, **summary}
                sources.append({"source": "confirmed pool observation headers", "path": str(header_path),
                                "sha256": sha256(header_path), "summary": cfg["_pool_observation_summary"]})
                checks.append(Check("pool_observations", "WARN", f"{len(pool_observations)} evidenced end-of-block observations; complete intervening pool logs, assumed block reception delay"))
            except (ValueError, OSError, KeyError, TypeError) as exc:
                pool_observations = []
                checks.append(Check("pool_observations", "BLOCKED", f"Observation evidence rejected: {exc}"))
        else:
            checks.append(Check("pool_observations", "WARN", "Observation headers not prepared; conservative last-event freshness retained"))
    # Gas features deduplicate these receipts by transaction hash.
    from .costs import calibrate_cost_model, deduplicate_receipts, value_transaction_costs
    receipts = value_transaction_costs(receipts, references, max_price_age_seconds=cfg["timing"]["max_price_age_seconds"])
    receipt_index = {row["transaction_hash"].lower(): row for row in deduplicate_receipts(receipts)}
    for event in events:
        receipt = receipt_index.get(event["transaction_hash"].lower())
        if receipt:
            event["gas_used"] = receipt["gas_used"]
            event["effective_gas_price"] = receipt["effective_gas_price"]
    grid = decisions(cfg)
    feature_config = {**cfg, "_onchain_coverage_complete": onchain_complete,
                      "_gas_receipts_sampled": mode == "real" and bool(receipt_index)}
    features = build_features(grid, events, references,
                              states if pool_observations is None else pool_observations, feature_config)
    cost_mode = cfg.get("costs", {}).get("mode", "missing")
    cost_model = calibrate_cost_model(receipts) if cost_mode == "empirical" else cfg.get("costs", {}) if cost_mode == "sensitivity" else {}
    outcomes = evaluate_episodes(grid, events, references, states, segments, cfg,
                                 cost_model=cost_model, pool_observations=pool_observations)
    computed = [row for row in outcomes if row.get("end_value_ex_fees_usdc") is not None or row.get("v_end_ex_fees_usdc") is not None]
    temporal_complete = bool(features) and all(row["status"] != "BLOCKED" for row in features)
    fee_complete = bool(outcomes) and all(row.get("fee_status") not in (None, "BLOCKED", "FAIL") for row in outcomes)
    cost_complete = bool(outcomes) and all(row.get("cost_total_usdc") is not None for row in outcomes)
    outcome_complete = bool(outcomes) and len(computed) == len(outcomes) and all(row["status"] != "BLOCKED" for row in outcomes)
    checks.extend([
        Check("temporal_availability", ("PASS" if mode == "synthetic" else "WARN") if temporal_complete else "BLOCKED",
              f"{sum(row['status'] != 'BLOCKED' for row in features)}/{len(features)} feature rows meet backward-join freshness rules; explicit reception assumptions; leakage tests are separate software evidence"),
        Check("position_fees", "WARN" if fee_complete and (segments or onchain_complete) else "BLOCKED",
              "Marginal swap-segment fee approximation; endpoint call modes and position fee-growth settlement remain unvalidated" if segments and fee_complete else
              "Complete event coverage contains no fee-generating activity; zero episode fees are supported by the initialized stream" if onchain_complete and fee_complete else "Missing or blocked episode fee evidence; missing fees remain null"),
        Check("costs", "WARN" if cost_complete else "BLOCKED",
              ("Explicit gas/conversion sensitivity is complete; empirical primary costs unavailable" if cost_complete else
               "Sensitivity parameters are configured but episode costs could not be evaluated; empirical primary costs unavailable") if cost_mode == "sensitivity" else
              f"Receipt-derived cost calibration: {cost_model.get('sample_counts', {})}; uncalibrated conversion or unavailable gas keeps costs missing"),
        Check("outcomes", "WARN" if outcome_complete else "BLOCKED",
              f"{len(computed)}/{len(outcomes)} episodes have principal values; primary Y needs validated fees and empirical costs"),
    ])
    tables = {"events": events, "states": states, "swap_segments": segments, "transaction_costs": receipts,
              "pool_observations": pool_observations or [],
              "reference_prices": references, "features": features, "outcomes": outcomes}
    for name, rows in tables.items():
        write_table(output / f"{name}.parquet", rows, empty_columns=("timestamp", "status"))
    status = final_status(checks, cfg["validation"]["critical_checks"], mode)
    validation = {"mode": mode, "status": status, "empirical_end_to_end": mode == "real" and status == "PASS", "checks": serialize(checks)}
    manifest = {"mode": mode, "scope": "full",
                "empirical": mode == "real", "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                "config_sha256": cfg["_config_sha256"],
                "required_interval": list(required_interval(cfg)), "query_parameters": {"pool": cfg["pool"], "pilot": cfg["pilot"], "timing": cfg["timing"]},
                "sources": sources, "cost_model": cost_model, "diagnostics": observed_diagnostics(cfg, tables, onchain_complete), "row_counts": {name: len(rows) for name, rows in tables.items()},
                "artifacts": {f"{name}.parquet": sha256(output / f"{name}.parquet") for name in tables},
                "checks": validation["checks"], "status": status}
    write_json(output / "manifest.json", manifest)
    report = render_report(cfg, validation, manifest)
    report_root = Path(cfg["_root"]) / "reports"
    report_path = report_root / "pilot_report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report)
    return {"mode": mode, "status": status, "empirical_end_to_end": validation["empirical_end_to_end"],
            "rows": manifest["row_counts"], "output": str(output), "report": str(report_path)}



def observed_diagnostics(cfg: dict, tables: dict, onchain_complete: bool) -> dict:
    """Coverage and summary statistics for the report."""
    first, last = required_interval(cfg)
    refs = tables["reference_prices"]
    stamps = sorted({float(row["timestamp"]) for row in refs})
    expected = set(range((first // 60 + 1) * 60, last, 60))
    observed = {int(stamp) for stamp in stamps}
    missing = sorted(expected - observed)
    longest_missing = current = 0
    previous = None
    for timestamp in missing:
        current = current + 1 if previous is not None and timestamp == previous + 60 else 1
        longest_missing = max(longest_missing, current)
        previous = timestamp

    def summary(values):
        present = [float(value) for value in values if value is not None]
        return {"count": len(present), "minimum": min(present) if present else None,
                "median": median(present) if present else None,
                "maximum": max(present) if present else None}

    return {"onchain_extraction_complete": onchain_complete,
            "pool_observations": cfg.get("_pool_observation_summary"),
            "historical_rpc_checkpoint_comparisons": cfg.get("_checkpoint_comparisons", []),
            "reference_minute_coverage": {"expected_count": len(expected), "observed_unique_count": len(stamps),
                                          "missing_count": len(missing),
                                          "longest_missing_minutes": longest_missing},
            "dex_cex_deviation_bps": summary(row.get("dex_cex_deviation_bps") for row in tables["features"]),
            "max_liquidity_share": summary(row.get("max_liquidity_share") for row in tables["outcomes"]),
            "feature_nonmissing_counts": {key: sum(row.get(key) is not None for row in tables["features"])
                                          for key in ("trailing_return", "realized_volatility", "swap_volume_usdc", "swap_count",
                                                      "active_liquidity", "gas_price_mean_gwei", "dex_cex_deviation_bps",
                                                      "absolute_dex_cex_deviation_bps", "volume_imbalance", "historical_markout_5m_bps")},
            "primary_y_nonmissing": sum(row.get("y_usdc") is not None for row in tables["outcomes"]),
            "sensitivity_y_nonmissing": sum(row.get("sensitivity_y_usdc") is not None for row in tables["outcomes"])}


def render_report(cfg: dict, validation: dict, manifest: dict) -> str:
    """Summarize the pilot; individual results remain in the current run tables."""
    mode = validation["mode"]
    diagnostics = manifest.get("diagnostics", {})
    counts = manifest["row_counts"]
    source = "deterministic synthetic fixture" if mode == "synthetic" else "Ethereum RPC"
    start, end = (datetime.fromtimestamp(stamp, timezone.utc).isoformat() for stamp in manifest["required_interval"])
    coverage = diagnostics.get("reference_minute_coverage", {})
    reference = ("Reference source: artificial minute data. No Binance observations are used in this synthetic run."
                 if mode == "synthetic" else
                 "Reference source: Binance closed 1m ETHUSDT / USDCUSDT, with publisher checksums."
                 if counts["reference_prices"] else "Reference source: unavailable.")
    lines = ["# Pilot results", "",
             f"Mode: **{mode}**; scope: **{manifest.get('scope', 'full')}**; status: **{validation['status']}**.",
             f"End-to-end validation: **{'PASS' if validation['empirical_end_to_end'] else 'NOT ESTABLISHED'}**. {counts['features']} decisions and {counts['outcomes']} fixed-range scenarios.", "",
             "## Data", "",
             f"Decisions: {cfg['pilot']['start']} to {cfg['pilot']['end']} (exclusive).",
             f"Extraction: {start} to {end} (exclusive), including {cfg['pilot']['warmup_hours']}h warmup and the outcome tail.",
             f"On-chain source: {source}. Development data, not a final holdout.", "",
             "Rows: " + "; ".join(f"{name} {count:,}" for name, count in counts.items()) + ".",
             reference,
             f"Minute coverage: {coverage.get('observed_unique_count', 0)} observations, {coverage.get('missing_count', 0)} missing boundaries; longest gap {coverage.get('longest_missing_minutes', 0)} minutes.", "",
             "## Checks", "", "| Check | Status | Detail |", "|---|---|---|"]
    for check in validation["checks"]:
        detail = str(check["detail"]).replace("|", "/").replace("\n", " ")
        lines.append(f"| {check['name']} | {check['status']} | {detail} |")
    available = diagnostics.get("feature_nonmissing_counts", {})
    complete = sum(count == counts["features"] for count in available.values())
    lines.extend(["", "## Results", "",
                  f"{complete}/{len(available)} feature fields are available at every decision. Primary Y: **{diagnostics.get('primary_y_nonmissing', 0)}/{counts['outcomes']}**; sensitivity Y: **{diagnostics.get('sensitivity_y_nonmissing', 0)}/{counts['outcomes']}**."])
    if complete != len(available):
        lines.append("Available decisions by field: " + "; ".join(f"{name} {count}/{counts['features']}" for name, count in available.items()) + ".")
    for key, label in (("dex_cex_deviation_bps", "DEX/reference deviation (bps)"), ("max_liquidity_share", "Hypothetical/historical liquidity share")):
        values = diagnostics.get(key, {})
        lines.append(f"{label}, min/median/max: {values.get('minimum')} / {values.get('median')} / {values.get('maximum')}.")
    comparisons = diagnostics.get("historical_rpc_checkpoint_comparisons", [])
    lines.extend(["Market-price differences are not reconstruction errors; individual flags remain in the feature/outcome tables.", "",
                  "## Historical checkpoints", "",
                  f"{sum(row['status'] == 'PASS' for row in comparisons)}/{len(comparisons)} exact pool-state comparisons. Separate hash-pinned calls use the same provider as the logs; these check state and global fee growth, not arbitrary position fees.", "",
                  "| Block | Exact comparisons | Largest absolute difference |", "|---|---:|---:|"])
    for block in sorted({row["block_number"] for row in comparisons}):
        rows = [row for row in comparisons if row["block_number"] == block]
        differences = [abs(int(row["difference"])) for row in rows if row["difference"] is not None]
        largest = max(differences) if len(differences) == len(rows) else "missing"
        lines.append(f"| {block} | {sum(row['status'] == 'PASS' for row in rows)}/{len(rows)} | {largest} |")
    if not comparisons:
        lines.append("| No historical comparisons | 0 | unavailable |")
    lines.extend(["", "## Assumptions and limits", "",
                  f"- Reception delays: block {cfg['timing']['block_delay_seconds']}s, reference {cfg['timing']['reference_delay_seconds']}s; execution delay {cfg['timing']['execution_delay_seconds']}s. Maximum ages: reference {cfg['timing']['max_price_age_seconds']}s, pool observation {cfg['timing']['max_pool_age_seconds']}s.",
                  "- Pool observations require evidenced headers and complete intervening logs. Warmup does not replace historical initialization.",
                  "- Decision-time ranges stay fixed, with independent episode capital. Fees are held separately; WETH fees retain price exposure.",
                  "- Production fees remain a marginal segment approximation. The separate three-case position check does not validate all hypothetical ranges. Endpoint-conditioned swaps may carry a flagged one-unit output cap.",
                  f"- Protocol state tolerance: 0; analytical relative tolerance: {cfg['validation']['reference_relative_tolerance']}; small-LP limit: {cfg['validation']['small_lp_max_share']:.2%}.",
                  f"- Y uses left-side WETH principal on a {cfg['timing']['benchmark_seconds']}s grid, a discrete residual rather than exact continuous LVR. Gas features use sampled receipts. Costs cover separate operations and initial conversion; terminal holdings are marked. Missing empirical costs keep primary Y null.", "",
                  "Related checks: [feature validation](feature_validation.md), [reference diagnosis](reference_resolution.md), [position fees](position_fee_validation.md).", ""])
    lines.extend(["Reproduce from cached inputs:", "",
                  "```sh\n.venv/bin/python -m lp_pilot run --config configs/pilot.toml\n```"])
    return "\n".join(lines) + "\n"

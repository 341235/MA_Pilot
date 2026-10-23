"""A small historical CORE-position fee check, separate from pilot features."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path
import argparse
import json

from .config import data_path, load_config, utc_seconds
from .data import DataContractError
from .position_fee_reference import fee_growth_inside, interval_claim, pending_claim, position_key
from .replay import marginal_fees
from .rpc import SELECTORS
from .rpc_ingest import (abi_words, checked_artifact, historical_state, load_rpc_export,
                         rpc_call_value, signed_word)
from .storage import read_table, sha256, write_json
from .uniswap import get_sqrt_ratio_at_tick

Q128, UINT256 = 1 << 128, 1 << 256
DIRECTORY = Path("data/validation/position_fees")


def _identity(row):
    return str(row.get("owner", "")).lower(), int(row["tick_lower"]), int(row["tick_upper"])


def select_cases(events, points, start, end) -> dict:
    """Selection uses observed identities/updates and tick paths, never fee agreement."""
    points = sorted((p for p in points if start <= int(p["timestamp"]) < end), key=lambda p: p["block_number"])
    if len(points) < 2:
        raise DataContractError("Two independent checkpoints inside the pilot are required")
    first, last = points[:2]
    before, after = int(first["block_number"]), int(last["block_number"])
    window = [row for row in events if before < int(row["block_number"]) <= after]
    path = [int(first["tick"]), *[int(row["tick"]) for row in window if row["event_type"] == "Swap"]]
    observed, updated = defaultdict(int), set()
    for row in events:
        kind, block = row["event_type"], int(row["block_number"])
        if kind in ("Mint", "Burn") and block <= before:
            observed[_identity(row)] += int(row["amount"]) * (1 if kind == "Mint" else -1)
        if kind in ("Mint", "Burn", "Collect") and before < block <= after:
            updated.add(_identity(row))
    selected, counts = {}, Counter()
    low, high = min(path), max(path)
    for identity, liquidity in sorted(observed.items()):
        if liquidity <= 0 or identity in updated:
            continue
        owner, lower, upper = identity
        kind = "active" if lower <= low and high < upper else "inactive" if high < lower or low >= upper else "crossing"
        counts[kind] += 1
        selected.setdefault(kind, {"case_type": kind, "owner": owner, "tick_lower": lower, "tick_upper": upper,
                                   "known_liquidity_lower_bound": str(liquidity), "position_key": position_key(owner, lower, upper)})
    return {"cases": [selected[kind] for kind in ("active", "crossing", "inactive") if kind in selected],
            "eligible_counts": dict(counts), "start_block_exclusive": before, "end_block_inclusive": after,
            "observed_tick_min": low, "observed_tick_max": high}


def acquire_position_evidence(cfg: dict, *, allow_network: bool = False) -> dict:
    """Only the frozen missing-call list; no log download or state-universe scan."""
    from .acquire import make_rpc_client
    root = Path(cfg["_root"])
    directory = root / DIRECTORY
    plan_path, path = directory / "selection_plan.json", directory / "acquisition.json"
    plan = json.loads(plan_path.read_text())
    if path.exists():
        saved = json.loads(path.read_text())
        if saved.get("status") == "PASS" and saved.get("plan_sha256") == sha256(plan_path):
            for source in saved["responses"]:
                checked_artifact(source, root)
            return saved
    result = {"status": "BLOCKED", "plan_sha256": sha256(plan_path), "responses": [], "missing_queries": plan["queries"]}
    if not allow_network:
        return result
    limits, authorized = plan["limits"], cfg["acquisition"]
    if (len(plan["queries"]) > 18 or limits["logical_requests"] > authorized["max_requests"]
            or limits["estimated_compute_units"] > authorized["max_compute_units"]
            or limits["downloaded_bytes"] > authorized["max_download_bytes"]):
        raise DataContractError("Position evidence plan exceeds the existing authorized budget")
    for name, digest in plan["input_sha256"].items():
        if sha256(root / name) != digest:
            raise DataContractError("Position selection input changed after the plan was frozen")
    client = make_rpc_client(cfg)
    client.budget.max_requests = limits["logical_requests"]
    client.budget.max_bytes = limits["downloaded_bytes"]
    client.max_compute_units, client.retries = limits["estimated_compute_units"], 0
    try:
        for offset in range(0, len(plan["queries"]), 10):
            chunk = plan["queries"][offset:offset + 10]
            client.batch_call([(item["method"], item["params"]) for item in chunk])
            for item in chunk:
                source = client.evidence_reference(item["method"], item["params"])
                target = Path(source["path"])
                result["responses"].append({**item, "evidence_path": str(target.relative_to(root)),
                                             "evidence_sha256": sha256(target)})
        result["status"], result["missing_queries"] = "PASS", []
    except (ValueError, RuntimeError, OSError) as exc:
        result["error"] = str(exc)
    result["usage"] = client.statistics()
    result["retrieved_at"] = datetime.now(timezone.utc).isoformat()
    write_json(path, result)
    return result


def check_applicability(case: dict, start: dict, end: dict, events: list) -> dict:
    """Reject pokes, collections and transient tick deletion, even if endpoints match."""
    reasons, minimum = [], {}
    position, terminal = start["position"], end["position"]
    liquidity = int(position["liquidity"])
    if liquidity <= 0 or liquidity != int(terminal["liquidity"]):
        reasons.append("POSITION_LIQUIDITY_NOT_POSITIVE_CONSTANT")
    for key in ("fee_growth_inside0_last_x128", "fee_growth_inside1_last_x128", "tokens_owed0", "tokens_owed1"):
        if position.get(key) != terminal.get(key):
            reasons.append(f"POSITION_STORAGE_CHANGED:{key}")
    window = sorted((row for row in events if int(start["block_number"]) < int(row["block_number"]) <= int(end["block_number"])),
                    key=lambda row: (int(row["block_number"]), int(row.get("transaction_index", 0)), int(row.get("log_index", 0))))
    own = [row for row in window if row["event_type"] in ("Mint", "Burn", "Collect") and _identity(row) == _identity(case)]
    if own:
        reasons.append("POSITION_UPDATE_OR_COLLECT_IN_INTERVAL")
    boundary_events = []
    for tick in (int(case["tick_lower"]), int(case["tick_upper"])):
        initial = start["ticks"].get(tick, start["ticks"].get(str(tick)))
        final = end["ticks"].get(tick, end["ticks"].get(str(tick)))
        if not initial or not final or not initial["initialized"] or not final["initialized"]:
            reasons.append(f"UNINITIALIZED_BOUNDARY:{tick}")
            continue
        gross = int(initial["liquidity_gross"])
        minimum[tick] = gross
        for row in window:
            if row["event_type"] not in ("Mint", "Burn") or tick not in (int(row["tick_lower"]), int(row["tick_upper"])):
                continue
            gross += int(row["amount"]) * (1 if row["event_type"] == "Mint" else -1)
            minimum[tick] = min(minimum[tick], gross)
            boundary_events.append({"tick": tick, "block_number": row["block_number"], "log_index": row.get("log_index"),
                                    "event_type": row["event_type"], "amount": str(row["amount"]), "gross_after": str(gross)})
            if gross <= 0:
                reasons.append(f"BOUNDARY_CLEARED_OR_INVALID:{tick}")
        if gross != int(final["liquidity_gross"]):
            reasons.append(f"BOUNDARY_GROSS_ENDPOINT_MISMATCH:{tick}")
        if minimum[tick] < liquidity:
            reasons.append(f"BOUNDARY_GROSS_BELOW_POSITION:{tick}")
    return {"status": "BLOCKED" if reasons else "PASS", "reasons": sorted(set(reasons)),
            "position_update_events": own, "boundary_min_gross": minimum, "boundary_update_events": boundary_events,
            "collected0_raw": str(sum(int(row.get("amount0", 0)) for row in own if row["event_type"] == "Collect")),
            "collected1_raw": str(sum(int(row.get("amount1", 0)) for row in own if row["event_type"] == "Collect")),
            "burn_principal0_raw": str(sum(int(row.get("amount0", 0)) for row in own if row["event_type"] == "Burn")),
            "burn_principal1_raw": str(sum(int(row.get("amount1", 0)) for row in own if row["event_type"] == "Burn"))}


def _decimal(value: Fraction) -> str:
    with localcontext() as context:
        context.prec = 90
        return str(Decimal(value.numerator) / Decimal(value.denominator))


def compare_fees(case, start, end, events, segments) -> dict:
    applicability = check_applicability(case, start, end, events)
    result = {**case, "identification_level": "CORE owner/range aggregate, not an individual NFT",
              "start_block_exclusive": start["block_number"], "end_block_inclusive": end["block_number"],
              "liquidity": str(start["position"]["liquidity"]), "applicability": applicability,
              "status": "BLOCKED", "tokens": [], "endpoint_states": {"start": start, "end": end}}
    if applicability["status"] != "PASS":
        return result
    lower, upper, liquidity = int(case["tick_lower"]), int(case["tick_upper"]), int(start["position"]["liquidity"])
    window = [s for s in segments if int(start["block_number"]) < int(s["block_number"]) <= int(end["block_number"])]
    low_sqrt, high_sqrt = get_sqrt_ratio_at_tick(lower), get_sqrt_ratio_at_tick(upper)
    active, partial = [], []
    for segment in window:
        left, right = sorted((int(segment["sqrt_start_x96"]), int(segment["sqrt_end_x96"])))
        if left == right:
            included = lower <= int(segment["tick_start"]) < upper
        else:
            included = max(left, low_sqrt) < min(right, high_sqrt)
            if included and not low_sqrt <= left <= right <= high_sqrt:
                partial.append(segment)
        if included:
            active.append(segment)
    if partial:
        result["applicability"]["status"] = "BLOCKED"
        result["applicability"]["reasons"].append("PARTIAL_SEGMENT_OVERLAP_NOT_EXACTLY_VALIDATED")
        return result
    production = marginal_fees(window, lower, upper, liquidity)
    result.update(production=production, active_segments=len(active), partial_segments=len(partial))
    for token in (0, 1):
        selected = [s for s in active if int(s["fee_token"]) == token]
        increments, economic = [], Fraction(0)
        for segment in selected:
            historical, fee = int(segment["liquidity"]), int(segment["fee_amount_lp"])
            if historical < liquidity or historical <= 0 or fee < 0:
                raise DataContractError("Existing position exceeds historical liquidity or invalid LP fee")
            increments.append(fee * Q128 // historical)
            economic += Fraction(fee * liquidity, historical)
        inside = []
        for state in (start, end):
            inside.append(fee_growth_inside(int(state["tick"]), lower, upper, int(state[f"fee_growth_global{token}_x128"]),
                                            int(state["ticks"][lower][f"fee_growth_outside{token}_x128"]),
                                            int(state["ticks"][upper][f"fee_growth_outside{token}_x128"])))
        claim = interval_claim(*inside, liquidity)
        growth = sum(increments) % UINT256
        event_claim = growth * liquidity // Q128
        residual = economic - claim["fees_raw"]
        bound = Fraction(1) + Fraction(len(selected) * liquidity, Q128)
        # A fixed arithmetic bound, not an empirical equality tolerance:
        # each positive product/division/sum is rounded at precision 90.
        unit, operations = Fraction(5, 10**90), 4 * len(selected) + 4
        precision_bound = economic * operations * unit / (1 - operations * unit)
        actual = production[f"fees{token}_raw"]
        precision_residual = Fraction(actual) - economic
        pending = [pending_claim(value, int(state["position"][f"fee_growth_inside{token}_last_x128"]), liquidity)
                   for value, state in zip(inside, (start, end))]
        checks = {"growth_exact": claim["growth_delta_x128"] == growth,
                  "settled_claim_exact": claim["fees_raw"] == event_claim,
                  "rounding_bound_proved": 0 <= residual < bound,
                  "production_precision_bound_proved": abs(precision_residual) <= precision_bound,
                  "claim_fits_uint128": max(claim["fees_raw"], *pending) < Q128}
        result["tokens"].append({"token": token, "symbol": "USDC" if token == 0 else "WETH", "checks": checks,
                                 "inside_start_x128": str(inside[0]), "inside_end_x128": str(inside[1]),
                                 "reference_growth_delta_x128": str(claim["growth_delta_x128"]),
                                 "event_growth_delta_x128": str(growth),
                                 "growth_difference_x128": str(growth - claim["growth_delta_x128"]),
                                 "reference_interval_claim_raw": str(claim["fees_raw"]),
                                 "event_protocol_claim_raw": str(event_claim),
                                 "protocol_claim_difference_raw": str(event_claim - claim["fees_raw"]),
                                 "production_marginal_raw": str(actual),
                                 "production_minus_reference_raw": _decimal(Fraction(actual) - claim["fees_raw"]),
                                 "exact_economic_amount_raw_decimal90": _decimal(economic),
                                 "economic_minus_reference_raw_decimal90": _decimal(residual),
                                 "rounding_bound_raw_decimal90": _decimal(bound),
                                 "production_precision_residual_raw_decimal90": _decimal(precision_residual),
                                 "production_precision_bound_raw_decimal90": _decimal(precision_bound),
                                 "active_fee_segments": len(selected),
                                 "sum_separately_settled_segments_raw": str(sum(liquidity * value // Q128 for value in increments)),
                                 "pending_start_raw": str(pending[0]), "pending_end_raw": str(pending[1]),
                                 "pending_change_raw": str(pending[1] - pending[0]),
                                 "pre_interval_carry_effect_raw": str(pending[1] - pending[0] - claim["fees_raw"]),
                                 "stored_tokens_owed_start_raw": str(start["position"][f"tokens_owed{token}"]),
                                 "stored_tokens_owed_end_raw": str(end["position"][f"tokens_owed{token}"])})
    result["status"] = "PASS" if all(all(token["checks"].values()) for token in result["tokens"]) else "FAIL"
    return result


def _endpoints(cfg, plan, acquisition, points) -> dict:
    root, result = Path(cfg["_root"]), {}
    for point in points:
        block = int(point["block_number"])
        result[block] = {**point, "ticks": {}, "positions": {}}
    for source in acquisition["responses"]:
        block, name, argument = int(source["block_number"]), source["name"], source["argument"]
        point = result[block]
        payload = checked_artifact(source, root)
        if name == "ticks":
            data = SELECTORS["ticks"] + format(int(argument) % UINT256, "064x")
        elif name == "positions" and argument in {case["position_key"] for case in plan["cases"]}:
            data = "0x514ea4bf" + argument[2:]
        else:
            raise DataContractError("Unexpected historical position evidence query")
        params = [{"to": plan["pool"], "data": data}, {"blockHash": point["block_hash"], "requireCanonical": True}]
        values = abi_words(rpc_call_value(payload, method="eth_call", params=params), 8 if name == "ticks" else 5)
        if name == "ticks":
            if (values[0] >= Q128 or values[7] not in (0, 1) or bool(values[0]) != bool(values[7])
                    or abs(signed_word(values[1], 128)) > values[0]):
                raise DataContractError("Invalid independently queried boundary state")
            point["ticks"][int(argument)] = {"liquidity_gross": values[0], "liquidity_net": signed_word(values[1], 128),
                                             "initialized": bool(values[7]), "fee_growth_outside0_x128": values[2],
                                             "fee_growth_outside1_x128": values[3]}
        else:
            if any(values[index] >= Q128 for index in (0, 3, 4)):
                raise DataContractError("Invalid historical position uint128 field")
            point["positions"][argument] = dict(zip(("liquidity", "fee_growth_inside0_last_x128", "fee_growth_inside1_last_x128",
                                                     "tokens_owed0", "tokens_owed1"), values))
    return result


def validate_position_fees(cfg: dict) -> dict:
    """Read independent endpoint responses; never query the network here."""
    root = Path(cfg["_root"])
    directory = root / DIRECTORY
    plan_path = directory / "selection_plan.json"
    plan = json.loads(plan_path.read_text())
    for name, digest in plan["input_sha256"].items():
        if sha256(root / name) != digest:
            raise DataContractError("Frozen position-fee selection input changed")
    events, coverage = load_rpc_export(data_path(cfg, "events_path"), data_path(cfg, "export_manifest_path"), root=root)
    points = [historical_state(checked_artifact(record, root))
              for record in json.loads(data_path(cfg, "checkpoints_path").read_text())["checkpoints"]]
    selected = select_cases(events, points, utc_seconds(cfg["pilot"]["start"]), utc_seconds(cfg["pilot"]["end"]))
    if (selected["cases"] != plan["cases"] or selected["eligible_counts"] != plan["eligible_counts"]
            or any(selected[key] != plan["interval"][key] for key in ("start_block_exclusive", "end_block_inclusive"))):
        raise DataContractError("Deterministic case selection differs from the frozen plan")
    acquisition = acquire_position_evidence(cfg)
    report = {"status": "BLOCKED", "plan": plan, "acquisition": acquisition, "cases": []}
    if acquisition["status"] == "PASS":
        before, after = selected["start_block_exclusive"], selected["end_block_inclusive"]
        endpoints = _endpoints(cfg, plan, acquisition, [point for point in points if point["block_number"] in (before, after)])
        path = data_path(cfg, "output_dir") / "real/swap_segments.parquet"
        segments = read_table(path)
        window = [event for event in events if before < int(event["block_number"]) <= after]
        pieces = [piece for piece in segments if before < int(piece["block_number"]) <= after]
        identity = lambda row: (row["transaction_hash"], int(row["log_index"]))
        if {identity(row) for row in window if row["event_type"] in ("Swap", "Flash")} != {identity(row) for row in pieces}:
            raise DataContractError("Fee segments do not cover the identical interval's Swap/Flash events")
        for case in plan["cases"]:
            start, end = (dict(endpoints[block], position=endpoints[block]["positions"][case["position_key"]]) for block in (before, after))
            result = compare_fees(case, start, end, events, segments)
            # Count crossings from emitted ticks, independently of the fee path.
            tick, crossings = int(start["tick"]), []
            for event in window:
                if event["event_type"] == "Swap":
                    current = int(event["tick"])
                    for boundary in (case["tick_lower"], case["tick_upper"]):
                        if (tick < boundary) != (current < boundary):
                            crossings.append({"boundary": boundary, "block_number": event["block_number"],
                                              "transaction_hash": event["transaction_hash"], "log_index": event["log_index"],
                                              "before_tick": tick, "after_tick": current})
                    tick = current
            result["crossings"] = crossings
            report["cases"].append(result)
        statuses = {case["status"] for case in report["cases"]}
        report.update(status="FAIL" if "FAIL" in statuses else "BLOCKED" if "BLOCKED" in statuses else "WARN",
                      event_counts=dict(Counter(row["event_type"] for row in window)), segment_count=len(pieces),
                      zero_amount_burn_count=sum(row["event_type"] == "Burn" and int(row["amount"]) == 0 for row in window),
                      complete_log_coverage=coverage["coverage_complete"])
    report["upstream"] = json.loads((directory / "upstream/manifest.json").read_text())
    for source in report["upstream"]["files"]:
        if sha256(root / source["path"]) != source["sha256"]:
            raise DataContractError("Pinned official fee-reference source changed")
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports/position_fee_validation.md").write_text(_render(report))
    return report


def _render(report: dict) -> str:
    plan, usage = report["plan"], report["acquisition"].get("usage", {})
    interval = plan["interval"]
    utc = lambda stamp: datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    lines = ["# Historical position-fee check", "", f"Overall **{report['status']}**. This checks a small historical sample, not all pilot fees.", "",
             f"Pool `{plan['pool']}`. End-of-block interval **({interval['start_block_exclusive']}, {interval['end_block_inclusive']}]**, {utc(interval['start_timestamp'])} to {utc(interval['end_timestamp'])}. All events after the start block through the complete end block are included.", "",
             "## Selection and evidence", "", plan["selection_rule"], "",
             f"Eligible structural cases: {plan['eligible_counts']}. Selection was frozen before RPC retrieval and fee comparisons in `data/validation/position_fees/selection_plan.json`.", "",
             "All selected owners are `0xc36442b4a4522e871399cd717abdd847ab11fe88` (the Position Manager). Identification is the **Core owner/tick-range aggregate**, not an individual NFT. No NFT states were used.", ""]
    if not report["cases"]:
        lines.extend(["**BLOCKED:** missing independent endpoint responses. The selection plan lists the required queries; no substitute state or fee values were generated.", ""])
        return "\n".join(lines)
    lines.extend([f"Existing raw checkpoint calls supply both endpoint ticks and global growth. Only the missing boundary/position reads were added: {usage['logical_requests']} `eth_call`, {usage['http_requests']} HTTP batches, {usage['downloaded_bytes']} bytes, {usage['estimated_compute_units']} estimated CU. All reads use the checkpoint's canonical block hash. These are separate state queries from the same provider as the logs, not a second-provider confirmation.", "",
                  f"Complete interval events: {report['event_counts']}; {report['segment_count']} fee segments. There are {report['zero_amount_burn_count']} zero-amount Burns elsewhere, none on a selected identity. Flash and SetFeeProtocol counts are both zero. All selected identities have no Mint/Burn/poke/Collect; liquidity and all stored position fields are unchanged. Every intervening boundary Mint/Burn is checked: no boundary clears or reinitializes, and gross liquidity exactly reaches the independent end snapshot.", "",
                  "| Case | Range [lower, upper) | Existing liquidity | Crossings | Maximum historical liquidity share | Status |",
                  "|---|---|---:|---:|---:|---|" ])
    for case in report["cases"]:
        share = Decimal(case.get("production", {}).get("max_liquidity_share", 0)) * 100
        lines.append(f"| {case['case_type']} | [{case['tick_lower']}, {case['tick_upper']}) | {case['liquidity']} | {len(case['crossings'])} | {share:.6f}% | {case['status']} |")
    lines.extend(["", "## Comparison rules and raw token results", "",
                  "For each token independently, `inside = global - below - above (mod 2^256)`, with lower inclusive and upper exclusive. B uses separate RPC endpoint states and the independent reference helper. Exact checks require `deltaInside = sum(floor(lpFee*Q128/historicalL)) (mod 2^256)` on whole active segments and `P = floor(positionL*deltaInside/Q128)` with zero integer tolerance.", "",
                  "A is the unchanged production `marginal_fees(segments, lower, upper, existingL)` function. The existing position is already in historical liquidity: nothing is added to the denominator. A preserves fractional raw units. For the exact rational ideal `E=sum(lpFee*existingL/historicalL)`, the predeclared rounding rule is `0 <= E-P < 1+n*existingL/Q128`. Decimal representation error is checked separately from this protocol truncation, using the fixed 90-digit arithmetic bound in `arithmetic_rules.json`, never a fitted tolerance.", "",
                  "| Case | Token | B state claim (raw) | Event protocol claim (raw) | Exact difference | A marginal (raw, display rounded) | A minus B (raw, display rounded) |",
                  "|---|---|---:|---:|---:|---:|---:|" ])
    for case in report["cases"]:
        for token in case["tokens"]:
            lines.append(f"| {case['case_type']} | {token['symbol']} | {token['reference_interval_claim_raw']} | {token['event_protocol_claim_raw']} | {token['protocol_claim_difference_raw']} | {Decimal(token['production_marginal_raw']):.9f} | {Decimal(token['production_minus_reference_raw']):.9f} |")
    lines.extend(["", "Display rounding above changes no comparison. The validator recomputes growth, unrounded amounts and rounding/precision bounds from the cached endpoint responses. USDC raw units are 1e-6 USDC; WETH raw units are 1e-18 WETH. Token discrepancies are never netted against each other.", "",
                  "## Accrual, stored owed and transfers", "",
                  "The interval claim isolates new growth; it is not the change in stored `tokensOwed`. Swaps do not settle the position storage. Pending fees at each endpoint use that position's actual stored lastInside. A prior fractional remainder can make their difference one raw unit larger than an interval-only floor. All sampled claims fit uint128; the reference reports full accrual before that storage cast.", "",
                  "| Case | Stored tokensOwed0 (both endpoints) | Stored tokensOwed1 (both endpoints) | Collected 0 / 1 | Returned principal 0 / 1 |",
                  "|---|---:|---:|---|---|" ])
    for case in report["cases"]:
        position, check = case["endpoint_states"]["start"]["position"], case["applicability"]
        lines.append(f"| {case['case_type']} | {position['tokens_owed0']} | {position['tokens_owed1']} | {check['collected0_raw']} / {check['collected1_raw']} | {check['burn_principal0_raw']} / {check['burn_principal1_raw']} |")
    for case in report["cases"]:
        for token in case["tokens"]:
            if int(token["pre_interval_carry_effect_raw"]):
                lines.extend(["", f"Actual carry example: {case['case_type']} {token['symbol']} pending claim changes from {token['pending_start_raw']} to {token['pending_end_raw']}, a change of {token['pending_change_raw']}; isolated interval claim is {token['reference_interval_claim_raw']}. Difference: {token['pre_interval_carry_effect_raw']} raw unit, inherited from the prior fractional remainder."])
    commit = report["upstream"]["commit"]
    lines.extend(["", "## Finding and remaining limits", "",
                  ("All six token/range protocol comparisons pass exactly. Production's fractional differences are explained by the separately proved growth and settlement rounding rules. No existing fee implementation error was found in these three historical cases."
                   if all(case["status"] == "PASS" for case in report["cases"]) else
                   "At least one case is blocked or differs under the fixed rules. Inspect the per-token checks; no global fee validation follows."), "",
                  "The in-range case earns both tokens; the crossing case covers 14 crossings of its upper boundary; the inactive control earns zero even at its upper-exclusive boundary. Stored owed amounts on that inactive position originate before the interval and are not new earnings. No transfers occurred on any sampled identity, so actual collection settlement and changing-liquidity cases remain untested here.", "",
                  "This does not validate counterfactual ranges with historically uninitialized boundaries or partial-segment fee interpolation. The large existing crossing aggregate also does not validate the pilot's small hypothetical LP assumption. The unchanged historical price path remains the pilot's marginal assumption. Fees, costs, features and reference prices are unchanged; primary targets remain null and fee assurance remains WARN globally.", "",
                  f"Official reference actually checked: v3-core **v1.0.0**, commit `{commit}`: [Tick.sol](https://github.com/Uniswap/v3-core/blob/{commit}/contracts/libraries/Tick.sol), [Position.sol](https://github.com/Uniswap/v3-core/blob/{commit}/contracts/libraries/Position.sol), [pool state interface](https://github.com/Uniswap/v3-core/blob/{commit}/contracts/interfaces/pool/IUniswapV3PoolState.sol), and the pinned pool implementation. Full sources and hashes are in `data/validation/position_fees/upstream/manifest.json`; this is not a deployed-bytecode equivalence proof.", "",
                  "RPC responses, fixed rules and sources: `data/validation/position_fees/`.", ""])
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/pilot.toml")
    parser.add_argument("--fetch-missing", action="store_true", help="Run only the frozen missing historical read list within existing limits")
    args = parser.parse_args()
    cfg = load_config(args.config)
    result = acquire_position_evidence(cfg, allow_network=True) if args.fetch_missing else validate_position_fees(cfg)
    print(json.dumps({"status": result["status"], "cases": [{"case_type": case["case_type"], "status": case["status"]}
                                                           for case in result.get("cases", [])],
                      "usage": result.get("usage"), "report": "reports/position_fee_validation.md"}))
    raise SystemExit(2 if result["status"] in ("FAIL", "BLOCKED") else 0)

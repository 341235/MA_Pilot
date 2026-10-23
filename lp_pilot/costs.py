"""Receipt gas costs and hypothetical cost scenarios."""
from __future__ import annotations

from decimal import Decimal
from statistics import median
from typing import Any, Iterable, Mapping

D = lambda value: Decimal(str(value))
WEI_PER_ETH = Decimal(10) ** 18


def deduplicate_receipts(receipts: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Deduplicate receipts by transaction, checking for conflicting values."""
    by_hash: dict[str, dict] = {}
    for source in receipts:
        row = dict(source)
        tx = str(row.get("transaction_hash", row.get("transactionHash", ""))).lower()
        if not tx:
            raise ValueError("Gas receipt requires a transaction hash")
        gas = row.get("gas_used", row.get("gasUsed"))
        price = row.get("effective_gas_price", row.get("effectiveGasPrice"))
        if isinstance(gas, str) and gas.startswith("0x"):
            gas = int(gas, 16)
        if isinstance(price, str) and price.startswith("0x"):
            price = int(price, 16)
        normalized = {**row, "transaction_hash": tx,
                      "gas_used": int(gas) if gas is not None else None,
                      "effective_gas_price": int(price) if price is not None else None}
        if tx in by_hash:
            previous = by_hash[tx]
            if (previous["gas_used"], previous["effective_gas_price"]) != (normalized["gas_used"], normalized["effective_gas_price"]):
                raise ValueError(f"Conflicting receipts for transaction {tx}")
            continue
        by_hash[tx] = normalized
    return [by_hash[key] for key in sorted(by_hash)]


def receipt_gas_eth(receipt: Mapping[str, Any]) -> Decimal | None:
    normalized = deduplicate_receipts([receipt])[0]
    if normalized["gas_used"] is None or normalized["effective_gas_price"] is None:
        return None
    if normalized["gas_used"] < 0 or normalized["effective_gas_price"] < 0:
        raise ValueError("Gas usage and gas price must be nonnegative")
    return D(normalized["gas_used"]) * D(normalized["effective_gas_price"]) / WEI_PER_ETH


def value_transaction_costs(receipts: Iterable[Mapping[str, Any]], references: Iterable[Mapping[str, Any]],
                            *, max_price_age_seconds: float = 120) -> list[dict]:
    """Value transaction gas using the last available external quote.

    Costs belong to the whole transaction; this does not allocate them to
    individual events or hypothetical positions.
    """
    from .temporal import asof_index, backward_asof
    refs = asof_index(references)
    output = []
    for receipt in deduplicate_receipts(receipts):
        row = dict(receipt)
        eth = receipt_gas_eth(row)
        at = row.get("timestamp")
        quote = backward_asof(refs, float(at), max_price_age_seconds, value_key="price_usdc_per_eth") if at is not None else {"value": None, "age_seconds": None}
        price = quote["value"]
        row.update(gas_cost_wei=None if eth is None else row["gas_used"] * row["effective_gas_price"],
                   gas_cost_eth=None if eth is None else str(eth),
                   gas_cost_usdc=float(eth * D(price)) if eth is not None and price is not None else None,
                   gas_reference_price_usdc_per_eth=str(price) if price is not None else None,
                   gas_reference_age_seconds=quote["age_seconds"],
                   transaction_cost_status="PASS" if eth is not None and price is not None else "BLOCKED",
                   cost_allocation_status="UNALLOCATED_TRANSACTION_COST")
        output.append(row)
    return output


def calibrate_cost_model(receipts: Iterable[Mapping[str, Any]]) -> dict:
    """Calibrate only unambiguously attributed single-operation transactions.

    Each receipt must specify ``operations`` as a one-element list and must state
    ``single_position=True``. A multicall containing mint/burn/collect together,
    or touching multiple positions, cannot be divided into event-level gas costs.
    """
    groups: dict[str, list[int]] = {name: [] for name in ("mint", "burn", "collect")}
    prices: list[int] = []
    rejected = 0
    for receipt in deduplicate_receipts(receipts):
        operations = receipt.get("operations", [])
        if len(operations) != 1 or operations[0] not in groups or receipt.get("single_position") is not True:
            rejected += 1
            continue
        if receipt["gas_used"] is None or receipt["effective_gas_price"] is None:
            rejected += 1
            continue
        groups[operations[0]].append(receipt["gas_used"])
        prices.append(receipt["effective_gas_price"])
    complete = all(groups.values()) and bool(prices)
    return {"mode": "empirical", **{f"{name}_gas": median(values) if values else None for name, values in groups.items()},
            "gas_price_gwei": median(prices) / 1e9 if prices else None,
            "sample_counts": {name: len(values) for name, values in groups.items()},
            "rejected_ambiguous_transactions": rejected,
            "token_conversion_bps": None,
            "status": "WARN" if complete else "BLOCKED",
            "flags": ["HYPOTHETICAL_OPERATION_COST_MODEL", "INVENTORY_CONVERSION_UNCALIBRATED"]}


def estimate_episode_costs(model: Mapping[str, Any] | None, capital_usdc: float | Decimal,
                           entry_eth_price: float | Decimal, exit_eth_price: float | Decimal) -> dict:
    """Estimate episode costs, retaining missing components as None.

    Entry conversion is charged on initial capital. Exit inventory is marked
    to market and retained, so there is no exit liquidation charge.
    """
    model = dict(model or {})
    mode = model.get("mode", "missing")
    sensitivity = mode == "sensitivity"
    flags = list(model.get("flags", []))
    if sensitivity:
        flags.append("COST_SENSITIVITY_NOT_EMPIRICAL")
    components: dict[str, float | None] = {}
    gas_price = model.get("gas_price_gwei")
    for operation, price in (("mint", entry_eth_price), ("burn", exit_eth_price), ("collect", exit_eth_price)):
        gas = model.get(f"{operation}_gas")
        value = None if gas is None or gas_price is None else D(gas) * D(gas_price) * D(price) / Decimal(10) ** 9
        if value is not None and value < 0:
            raise ValueError("Cost parameters must be nonnegative")
        components[f"{operation}_gas_usdc"] = float(value) if value is not None else None
    bps = model.get("token_conversion_bps")
    conversion = None if bps is None else D(capital_usdc) * D(bps) / Decimal(10_000)
    if conversion is not None and conversion < 0:
        raise ValueError("Conversion costs must be nonnegative")
    components["inventory_conversion_usdc"] = float(conversion) if conversion is not None else None
    complete = all(value is not None for value in components.values())
    total = sum(components.values()) if complete else None
    if not complete:
        flags.append("MISSING_IMPLEMENTATION_COSTS")
    return {**components, "total_usdc": total, "model_kind": mode,
            "empirical_total_usdc": total if mode == "empirical" else None,
            "sensitivity_total_usdc": total if sensitivity else None,
            "status": "BLOCKED" if not complete else "WARN",
            "flags": sorted(set(flags + ["NO_TERMINAL_CASH_LIQUIDATION", "COST_MODEL_USES_SEPARATE_OPERATIONS"]))}

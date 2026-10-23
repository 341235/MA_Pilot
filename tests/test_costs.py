"""Gas accounting with synthetic receipts."""
from decimal import Decimal

import pytest

from lp_pilot.costs import (calibrate_cost_model, deduplicate_receipts,
                            estimate_episode_costs, receipt_gas_eth,
                            value_transaction_costs)


def receipt(tx="0x1", **kwargs):
    return {"transaction_hash": tx, "gas_used": 21000, "effective_gas_price": 30_000_000_000, **kwargs}


def test_actual_receipt_valuation_is_backward_available_and_once_per_transaction():
    row = receipt(timestamp=100)
    prices = [{"timestamp": 90, "available_at": 91, "price_usdc_per_eth": 2000},
              {"timestamp": 99, "available_at": 101, "price_usdc_per_eth": 9000}]
    values = value_transaction_costs([row, row], prices)
    assert len(values) == 1
    assert values[0]["gas_cost_wei"] == 630_000_000_000_000
    assert Decimal(values[0]["gas_cost_eth"]) == Decimal("0.00063")
    assert values[0]["gas_cost_usdc"] == pytest.approx(1.26)
    assert values[0]["gas_reference_age_seconds"] == 10
    assert values[0]["cost_allocation_status"] == "UNALLOCATED_TRANSACTION_COST"
    missing = value_transaction_costs([row], prices, max_price_age_seconds=5)[0]
    assert missing["gas_cost_usdc"] is None
    assert missing["transaction_cost_status"] == "BLOCKED"


def test_gas_conversion_and_transaction_deduplication():
    row = receipt()
    assert receipt_gas_eth(row) == Decimal("0.00063")
    values = value_transaction_costs([receipt(timestamp=10), receipt(timestamp=10, log_index=1)],
                                    [{"timestamp": 10, "price_usdc_per_eth": 2500}])
    assert len(values) == 1
    assert values[0]["gas_cost_usdc"] == pytest.approx(1.575)
    assert receipt_gas_eth({"transactionHash": "0x1", "gasUsed": "0x5208", "effectiveGasPrice": "0x6fc23ac00"}) == Decimal("0.00063")


def test_missing_costs_are_not_zero_and_conflicts_rejected():
    assert estimate_episode_costs({}, 10000, 2500, 2500)["total_usdc"] is None
    assert receipt_gas_eth({"transaction_hash": "0x1"}) is None
    with pytest.raises(ValueError):
        deduplicate_receipts([receipt(), receipt(gas_used=22000)])


def test_sensitivity_and_empirical_columns_separate():
    model = {"mode": "sensitivity", "mint_gas": 250000, "burn_gas": 150000,
             "collect_gas": 100000, "gas_price_gwei": 30, "token_conversion_bps": 5}
    result = estimate_episode_costs(model, 10000, 2500, 2500)
    assert result["total_usdc"] == pytest.approx(42.5)
    assert result["sensitivity_total_usdc"] == 42.5
    assert result["empirical_total_usdc"] is None
    assert "COST_SENSITIVITY_NOT_EMPIRICAL" in result["flags"]


def test_multicall_cannot_be_assigned_to_individual_events():
    model = calibrate_cost_model([receipt(operations=["mint", "collect"], single_position=True)])
    assert model["mint_gas"] is None
    assert model["rejected_ambiguous_transactions"] == 1
    assert model["status"] == "BLOCKED"
    rows = [receipt(str(i), operations=[operation], single_position=True)
            for i, operation in enumerate(("mint", "burn", "collect"))]
    model = calibrate_cost_model(rows)
    assert model["mint_gas"] == 21000
    assert model["token_conversion_bps"] is None

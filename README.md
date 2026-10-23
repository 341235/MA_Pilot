# Uniswap v3 LP pilot

Feasibility code for *Dynamic Liquidity Provision in Uniswap v3: Evaluating
Order-Flow Toxicity Signals*. It reconstructs one historical pool and evaluates
fixed liquidity ranges at hourly decisions. The January 2024 sample is
development data; no model is trained and no transactions are sent.

## Setup and offline run

Python 3.11+ is required; development used Python 3.12.4.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pytest -q
.venv/bin/python -m lp_pilot run --config configs/pilot.toml
```

The run uses cached Alchemy RPC inputs and Binance ETHUSDT/USDCUSDT minute
archives. Acquisition instructions are in [alchemy.md](docs/alchemy.md).
Missing inputs block the calculation.

The current sample has 14,300 events, 48 decisions and 144 range outcomes.
Twenty state comparisons match separate historical calls exactly at four checkpoints,
using the same provider. Status remains `WARN`: hypothetical range fees are
approximate and empirical operation costs are missing, so primary targets
remain null. See the [pilot report](reports/pilot_report.md).

The supporting comparisons and study figures also run offline:

```sh
.venv/bin/python -m lp_pilot validate-features --config configs/pilot.toml
.venv/bin/python -m lp_pilot.reference_diagnostics --config configs/pilot.toml
.venv/bin/python -m lp_pilot.position_fee_validation --config configs/pilot.toml
.venv/bin/python -m scripts.plot_pilot_figures --config configs/pilot.toml
```

The reference comparison is fixed to 2024-01-15 with the required preceding
history. Its results and the three existing-position fee checks are reported
in `reports/`; plots are saved as PDF and PNG in `reports/figures/`.

## Model and files

[configs/pilot.toml](configs/pilot.toml) fixes the USDC/WETH 500-pip pool,
2024-01-15 through 2024-01-17 exclusive, one-hour holdings, 10,000 USDC capital
and widths 0.5%/1%/2%. Each range is centered at its decision and held fixed.
The marginal LP leaves the historical path unchanged. Reference conversion is
`ETH/USDC = ETHUSDT / USDCUSDT`, with a backward join and an age limit per leg.

The primary target is `Y = net LP P&L - discrete rebalancing P&L`, using
left-end principal WETH holdings on a minute grid. This is a discrete residual,
not exact continuous LVR. WETH fees retain price exposure. Missing empirical
costs keep primary targets null; sensitivity columns are separate. Decisions
are evaluated independently and do not form an equity curve.

Current tables and validation checks are in `data/runs/real/`. Input snapshots,
receipts and raw responses are retained for offline reproduction. Large protocol
integers are stored as decimal strings in Parquet. The latest run overwrites
`reports/pilot_report.md`; it does not archive old runs.

Details: [data contract](docs/data_contract.md),
[feature definitions](docs/feature_definitions.md), [outcomes](docs/outcomes.md),
[protocol math](docs/protocol.md), [sources](docs/sources.md).

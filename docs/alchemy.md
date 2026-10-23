# Alchemy acquisition

Set the complete endpoint in `ETH_RPC_URL` in the ignored `.env` file.
`ARCHIVE_RPC_URL`, when set, takes precedence for historical calls.
The commands acquire the sample fixed in `configs/pilot.toml`:

```sh
set -a
source .env
set +a
.venv/bin/python -m lp_pilot fetch-reference --config configs/pilot.toml
.venv/bin/python -m lp_pilot fetch-rpc --config configs/pilot.toml
.venv/bin/python -m lp_pilot prepare-observations --config configs/pilot.toml --allow-network
.venv/bin/python -m lp_pilot run --config configs/pilot.toml
```

Acquisition prints a summary and retains inputs in `data/input/rpc/` and
`data/cache/`. Observation preparation checks cached headers first and fetches
only missing evaluation cutoffs when `--allow-network` is set. Analysis itself
runs offline. `fetch-fine-reference` separately acquires the fixed aggregate-trade
comparison archives if needed.

## Coverage and state

Log queries cover contiguous ten-block intervals for all pool topics, including
successful empty responses. The loader checks normalized events against raw
responses and block headers. Errors are never interpreted as empty intervals.

Initialization is the end of the block immediately before replay. The snapshot
reads all 694 bitmap words (`-347..346`) and every initialized tick, including
gross/net liquidity and both fee-growth outside values. Zero words are retained;
out-of-range bits fail. Warmup cannot replace the initial tick distribution.

All state calls use EIP-1898 `{blockHash, requireCanonical: true}`, with matching
fresh headers before and after. ABI values, pool identity, liquidity bounds,
zero total net liquidity and active-liquidity sums must pass before the snapshot
is complete. Separate checkpoints compare price, tick, liquidity and both global
fee-growth values using exact integer equality. These are separate methods at
one provider, not cross-provider checks or validation of every position's fees.

An archived block header confirms a replayed state only with complete pool logs
through that block. This includes quiet blocks. `pool_observations.parquet`
keeps observation and last-change times separate; missing headers add no rows.

## Limits and current result

Batches contain at most ten logical calls. Every subrequest and retry counts
against request, byte and estimated-CU limits. Immutable responses are cached;
fresh canonicality headers bypass the cache. Endpoint credentials are redacted.
The configured ceilings are 40,000 logical calls, 500 MB, 1.5 million estimated
CU and 250 CU/s, with at most 5,000 initialized ticks and 100 sampled receipts.
These are local stopping limits, not account-quota or billing measurements.

The sample retains 14,300 events, 1,234 initialized ticks and 10,045 evidenced
observation headers. All 48 decisions and 144 sensitivity rows are available
under the 120-second freshness limit. Status remains `WARN`: primary targets
are null because position-fee and empirical-cost limitations remain.
See the [pilot report](../reports/pilot_report.md),
[data contract](data_contract.md) and [sources](sources.md).

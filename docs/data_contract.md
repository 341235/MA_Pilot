# Data contract

Times are UTC seconds; coverage is half-open `[start,end)` and includes warmup,
holding periods and markout tails. Missing real inputs remain missing.

## RPC inputs

[alchemy.md](alchemy.md) describes acquisition. Inputs live in `data/input/rpc/`.

| File | Required content |
| --- | --- |
| `events.parquet` | Canonically ordered, losslessly decoded pool logs |
| `export_manifest.json` | Interval, contiguous query windows, counts, hashes and raw response references |
| `snapshot.json` | Pre-event end-of-block state and complete initialized ticks |
| Snapshot manifest/evidence | Checksummed original calls, block identity, all bitmap words and ticks |
| `checkpoints.json` | Separate historical state calls with original evidence and checksums |
| `receipts.json` | Transaction hashes, gas used, effective gas price and raw receipt references |
| `observation_headers.json` | Checksummed raw headers, original export hash, sparse coverage and evaluation cutoffs |
| `access_probe.json` | Saved pool identity and token-decimal checks |

The loader reconciles every event against preserved responses and log-bearing
block headers. Coverage includes successful empty windows; absent/error responses
do not establish coverage. Canonical order is
`(block_number, transaction_index, log_index)`. Only pool-emitted logs belong in
the stream; position-manager copies would double count.

Decoded fields include block/transaction hashes, ordering indices, `event_type`,
raw `amount0/1`, `sqrt_price_x96`, liquidity and signed tick bounds. Swap amounts
use pool-balance signs. Mint, Burn, Swap, Collect, Flash, SetFeeProtocol,
CollectProtocol, Initialize and IncreaseObservationCardinalityNext are decoded;
unknown signatures remain `Unknown` for explicit downstream treatment.

Raw ABI integers never use floats or scientific notation. `storage.write_table`
stores protocol integer columns as decimal strings in Parquet; safe timestamps
and ordering columns remain integers. Readers parse protocol values as Python integers.

## Snapshots and checkpoints

Initialization is at `first_event_block - 1`. Required fields are block number,
hash and timestamp; `sqrt_price_x96`, tick, active liquidity, fee tier and spacing;
historical protocol-fee denominators; both global fee-growth values; and every
initialized tick's gross/net liquidity and fee-growth outside values. Tick rows
may be a list or dictionary keyed by decimal tick. Packed `fee_protocol` can
replace the two denominators. Historical zero values must be observed.

`ticks_complete=true` requires the saved evidence: all 694 valid bitmap words and
all set-bit ticks at one canonical block hash, valid ABI values, gross/net checks,
zero total net liquidity and agreement with active liquidity. Setting the flag
on a partial snapshot is insufficient. Every state call uses EIP-1898
`{blockHash, requireCanonical: true}` with matching fresh headers before/after.

Checkpoint records carry `source_kind="historical_rpc"`, `evidence_path` and
`evidence_sha256`. The pipeline checks the hash, decodes original calls and
compares price, tick, liquidity and both global fee-growth values exactly.
Event-derived copies are not independent checkpoint evidence. Separate calls to
one provider do not establish cross-provider authenticity or position-fee accuracy.

## Confirmed pool observations

`pool_observations.parquet` is derived separately from unchanged events and
replay states. Every row requires an actual archived header and complete pool
logs through its block. All log-bearing blocks require matching headers; missing
empty-block headers add no rows. `observed_at` is the header time,
`state_changed_at` the last relevant change, and `state_anchor_at` the initial
snapshot time. A change before initialization remains unknown (`null`).

Decision joins require observation and availability times at or before the
decision, including the assumed 12-second block delay. Retrospective inventory
valuation uses observations at or before its target. Both use the unchanged
120-second maximum age measured from query time to observation time; delay is
not subtracted from age. Header hashes and log completeness use one provider.

## Receipts

`receipts.json` accepts a list or `{"receipts": [...]}`. Required receipt fields
are `transaction_hash`, `gas_used` and `effective_gas_price`; standard RPC names
and hexadecimal integers are also accepted. Gas is `gas_used * effective_gas_price`
wei, counted once per hash. Conflicting duplicates fail.

`operations` and `single_position` record reviewed attribution. Only unambiguous
single-operation, single-position receipts can calibrate Mint/Burn/Collect gas.
The acquired sample uses `single_position=false`: multicall costs cannot simply
be divided among events. Transaction costs are converted to USDC with an available
backward reference price; a missing quote leaves the value null.

## Reference market

Binance ETHUSDT and USDCUSDT daily one-minute ZIPs are verified against publisher
SHA256 sidecars. Each day requires 1,440 unique minutes and expected endpoints.
Manifests preserve URL, retrieval/verification times, cache status, hash and
coverage. The sample includes 2024-01-14 through 2024-01-17 for both symbols.

Archives use milliseconds before 2025 and microseconds from 2025-01-01; loaders
check units, dates and candle length. A close becomes observable at `open_time+60`,
plus configured delay. `ETH/USDC = ETHUSDT / USDCUSDT` uses backward joins with a
maximum age for each leg; no silent parity substitution.

The finer comparison uses daily Spot `aggTrades` archives for both symbols,
with publisher checksums and exact source manifests. These are executed trade
prices, not order-book midprices. Loaders validate the eight columns, ordered
aggregate IDs, trade-ID ranges, positive prices/quantities, UTC dates and units
(milliseconds in the 2024 sample). Both legs are backward joined with the same
120-second age limit; their times and ages remain recorded. No future quote
repairs a missing endpoint. See [sources](sources.md) for references.

## Derived results

`data/runs/real/` contains the current replay, observations, references, features,
outcomes and sampled transaction costs. Its `manifest.json` records input/output
hashes, parameters and validation checks. Original snapshots and receipts stay
in the input directory. Repeated runs overwrite derived results.

The reference diagnosis retains one common-sample markout table for the study
figure, including Swap identities and weights. Other diagnostic intermediate
values are recomputed from the cache; findings are written to Markdown reports.

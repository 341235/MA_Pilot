# Sources

Documentation accessed on 2026-10-22. Release tags below are explicit version
references; they were not additionally resolved to immutable commit hashes.
Web documentation and `master` references are unpinned snapshots of that date.
The later position-fee check separately pins v3-core `v1.0.0` to commit
`e3589b192d0be27e100cd0daaf6c97204fdb1899`: Tick.sol, Position.sol, the pool state
interface and pool implementation. Exact source bytes, URLs and hashes are in
`data/validation/position_fees/upstream/manifest.json`; use and limits are in
[position_fee_validation.md](../reports/position_fee_validation.md).

| Source | Version / pages actually consulted | Use and validation scope |
| --- | --- | --- |
| [Uniswap v3-core](https://github.com/Uniswap/v3-core/tree/v1.0.0) | Release `v1.0.0` | Protocol reference, not a deployed-bytecode equivalence proof |
| [TickMath.sol](https://github.com/Uniswap/v3-core/blob/v1.0.0/contracts/libraries/TickMath.sol), [SqrtPriceMath.sol](https://github.com/Uniswap/v3-core/blob/v1.0.0/contracts/libraries/SqrtPriceMath.sol), [SwapMath.sol](https://github.com/Uniswap/v3-core/blob/v1.0.0/contracts/libraries/SwapMath.sol) | `v1.0.0` | Integer math and rounding; licensing and deviations in `docs/protocol.md` |
| [TickMath.spec.ts](https://github.com/Uniswap/v3-core/blob/v1.0.0/test/TickMath.spec.ts), [SqrtPriceMath.spec.ts](https://github.com/Uniswap/v3-core/blob/v1.0.0/test/SqrtPriceMath.spec.ts), [SwapMath.spec.ts](https://github.com/Uniswap/v3-core/blob/v1.0.0/test/SwapMath.spec.ts) | `v1.0.0` | Official numeric vectors selected for Python unit tests; upstream full suite not executed |
| [UniswapV3Pool.sol](https://github.com/Uniswap/v3-core/blob/v1.0.0/contracts/UniswapV3Pool.sol) | `v1.0.0` | Swap iteration, tick crossing, fee/protocol treatment |
| [TickBitmap.sol](https://github.com/Uniswap/v3-core/blob/v1.0.0/contracts/libraries/TickBitmap.sol), [core LICENSE](https://github.com/Uniswap/v3-core/blob/v1.0.0/LICENSE) | `v1.0.0` | Empty bitmap-word boundaries and original BUSL change-license conditions; attribution in translated modules and `docs/protocol.md` |
| [Pool events ABI](https://github.com/Uniswap/v3-core/blob/v1.0.0/contracts/interfaces/pool/IUniswapV3PoolEvents.sol) | `v1.0.0`, also inspected `main` | All nine event signatures and ABI field positions; topics derived by Ethereum Keccak-256, not SHA3-256 |
| [Pool state ABI](https://github.com/Uniswap/v3-core/blob/v1.0.0/contracts/interfaces/pool/IUniswapV3PoolState.sol), [pool immutables ABI](https://github.com/Uniswap/v3-core/blob/v1.0.0/contracts/interfaces/pool/IUniswapV3PoolImmutables.sol) | `v1.0.0`, also inspected `main` | Selectors, token order, protocol-fee encoding, bitmap/tick fields |
| [Periphery LiquidityAmounts](https://github.com/Uniswap/v3-periphery/blob/v1.0.0/contracts/libraries/LiquidityAmounts.sol), [NonfungiblePositionManager](https://github.com/Uniswap/v3-periphery/blob/v1.0.0/contracts/NonfungiblePositionManager.sol) | `v1.0.0` | Amount/range conventions; Collect can include previously burned principal, and manager events must not duplicate pool logs |
| [Periphery LiquidityAmounts on main](https://github.com/Uniswap/v3-periphery/blob/main/contracts/libraries/LiquidityAmounts.sol) | `main`, unpinned | Also consulted during implementation; an attempted `v1.4.4` reference returned 404 and was not used |
| [Ethereum JSON-RPC](https://ethereum.org/en/developers/docs/apis/json-rpc/) | Live official documentation | Read-only calls, historical block tags, logs, receipts, gas units |
| [Binance Public Data README](https://github.com/binance/binance-public-data/blob/master/README.md) | `master`, accessed 2026-10-22 | Daily spot paths, kline and aggregate-trade columns, checksum sidecars, ms/us timestamp change |
| [Binance Spot aggregate trades](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market#compressedaggregate-trades-list) | Live official documentation, accessed 2026-10-22 | Executed trades aggregated by matching taker order, time and price; field semantics only, no live REST trade retrieval |

Saved pool identity and token-decimal checks are in
`data/input/rpc/access_probe.json`. The historical inputs use Alchemy; separate
state calls and log replay share that provider.

## Alchemy RPC

Official references read on 2026-10-22:

- [Alchemy eth_getLogs](https://www.alchemy.com/docs/chains/ethereum/ethereum-api-endpoints/eth-get-logs):
  Free-plan log interval is ten blocks per query; query every disjoint window,
  including empty results, and retain response evidence.
- [Alchemy compute-unit costs](https://www.alchemy.com/docs/reference/compute-unit-costs):
  `eth_call` 26 CU, `eth_getLogs` 60 CU, block headers/receipts 20 CU;
  batch cost is the sum of every subrequest. The implementation uses these as
  local estimates, not account-quota or billing assertions.
- [Alchemy pricing plans](https://www.alchemy.com/docs/reference/pricing-plans):
  archive and Node API availability in the free tier; no upgrade or payment was
  performed for this pilot.
- [EIP-1898](https://eips.ethereum.org/EIPS/eip-1898): canonical block-hash
  identifiers for coherent historical contract calls. Actual batch and
  block-hash calls were tested through the configured Alchemy endpoint.

The active RPC route enumerates all 694 tick-bitmap words and each set-bit tick.
Checkpoint methods are separate but use the same provider. Acquisition summaries
are printed by the commands; input evidence and the current run manifest retain
the coverage, hashes and validation results.

The real 2024-01-14 through 2024-01-17 reference archives and publisher checksums were downloaded
from these public source directories:

- [ETHUSDT daily 1m](https://data.binance.vision/?prefix=data/spot/daily/klines/ETHUSDT/1m/)
- [USDCUSDT daily 1m](https://data.binance.vision/?prefix=data/spot/daily/klines/USDCUSDT/1m/)

The fixed finer-reference comparison uses 2024-01-14 history and 2024-01-15
Spot aggregate trades for both symbols:

- [ETHUSDT daily aggregate trades](https://data.binance.vision/?prefix=data/spot/daily/aggTrades/ETHUSDT/)
- [USDCUSDT daily aggregate trades](https://data.binance.vision/?prefix=data/spot/daily/aggTrades/USDCUSDT/)

These prices come from executions, not an order book. Conversion uses both
backward legs with a 120-second age limit and records their distinct times.
Downloads retain publisher checksums, sequence IDs, timestamp units and source
manifests; the 2024 files use milliseconds. Measurement results and delay
sensitivity are reported separately in `reports/feature_validation.md`.

Exact object URLs, checksums, counts, coverage and retrieval times are retained
in the access evidence and each generated data manifest. Binance archives can
be corrected upstream; checksum verification establishes integrity against the
retrieved sidecar, not immutability of the publisher's historical dataset.

The preliminary study supplies the research context. Pilot definitions remain
provisional; no thesis document was modified. Synthetic fixtures do not establish
empirical replay, fee, receipt-allocation or outcome accuracy.

# Reference-resolution diagnosis

Status: **WARN**. Fixed day: **2024-01-15 UTC**. Existing cache only; no network requests.

A = saved Binance minute archives; B = independently closed ETHUSDT and USDCUSDT minutes, then divided; C = existing aggTrade reference.

| Leg | Compared minutes | Exact closes | Price differences | Time errors | Missing trade / kline minutes |
|---|---:|---:|---:|---:|---|
| ETHUSDT | 1505 | 1505 | 0 | 0 | 0 / 0 |
| USDCUSDT | 1505 | 1505 | 0 | 0 | 0 / 0 |

Minutes are half-open UTC intervals `[u,u+60s)`. Validated chronological trades use `(raw millisecond timestamp, aggregate ID)` order; the last row wins, including timestamp ties. No empty minute is filled. Prices are compared as exact Decimals, with no tolerance.
Trade time and candle completion are separate. Raw kline close labels are `u+59.999s`; observation time is `u+60s` and assumed availability is `u+61s`. B uses the same candle observation age as A, preserving actual last-trade times separately.

Saved A versus independently converted B: 0 price/time mismatches; 0 B minutes absent from A.

| Pair | Feature | Common decisions | Median / p95 absolute difference (bps) | Sign agreement |
|---|---|---:|---|---|
| A/B | dex_cex_deviation_bps | 24 | 0 / 0 | 24/24 |
| A/B | absolute_dex_cex_deviation_bps | 24 | 0 / 0 | 24/24 |
| A/B | historical_markout_5m_bps | 24 | 0 / 0 | 24/24 |
| A/C | dex_cex_deviation_bps | 24 | 3.23261 / 10.7387 | 14/24 |
| A/C | absolute_dex_cex_deviation_bps | 24 | 1.53413 / 5.81699 | 24/24 |
| A/C | historical_markout_5m_bps | 24 | 3.98458 / 7.80934 | 9/24 |
| B/C | dex_cex_deviation_bps | 24 | 3.23261 / 10.7387 | 14/24 |
| B/C | absolute_dex_cex_deviation_bps | 24 | 1.53413 / 5.81699 | 24/24 |
| B/C | historical_markout_5m_bps | 24 | 3.98458 / 7.80934 | 9/24 |

Sign agreement for absolute deviations is mechanically nonnegative and supplies no directional validation.

From 24 fixed decisions, exclusions on the three-way common masks: {'dex_cex_deviation_bps': 0, 'absolute_dex_cex_deviation_bps': 0, 'historical_markout_5m_bps': 0}. No numerical comparison tolerance was introduced.

Mature Swap membership identical: **True**. A/B/C valid: 4527/4527/4527; common: 4527. Markouts are recomputed with the production formula on this three-way intersection using identical original `abs(amount0)/1e6` weights. Per-decision membership, exclusions and exact common volumes are saved.

| Variant | Mature Swaps | Immature Swaps | Missing / invalid endpoints |
|---|---:|---:|---:|
| A | 4527 | 363 | 0 |
| B | 4527 | 363 | 0 |
| C | 4527 | 363 | 0 |

## Fixed observations

Each row shows ETHUSDT / USDCUSDT. A and B select the same candle; actual last trades shown for B also explain A's matching closes.

| Decision UTC | Variant | Leg prices | Observation times UTC (ETH / USDC) | Available UTC (ETH / USDC) | Leg ages (s) | Actual last trades UTC |
|---|---|---|---|---|---|---|
| 00:00 | A | 2474.70000000 / 1.00070000 | 23:59:00 / 23:59:00 | 23:59:01 / 23:59:01 | 60.000 / 60.000 | 23:58:59.999000 / 23:58:59.921000 |
| 00:00 | B | 2474.70000000 / 1.00070000 | 23:59:00 / 23:59:00 | 23:59:01 / 23:59:01 | 60.000 / 60.000 | 23:58:59.999000 / 23:58:59.921000 |
| 00:00 | C | 2472.86 / 1.0008 | 23:59:58.327000 / 23:59:58.219000 | 23:59:59.327000 / 23:59:59.219000 | 1.673 / 1.781 | 23:59:58.327000 / 23:59:58.219000 |
| 12:00 | A | 2539.21000000 / 1.00050000 | 11:59:00 / 11:59:00 | 11:59:01 / 11:59:01 | 60.000 / 60.000 | 11:58:59.999000 / 11:58:59.931000 |
| 12:00 | B | 2539.21000000 / 1.00050000 | 11:59:00 / 11:59:00 | 11:59:01 / 11:59:01 | 60.000 / 60.000 | 11:58:59.999000 / 11:58:59.931000 |
| 12:00 | C | 2539.36 / 1.0006 | 11:59:58.843000 / 11:59:58.337000 | 11:59:59.843000 / 11:59:59.337000 | 1.157 / 1.663 | 11:59:58.843000 / 11:59:58.337000 |
| 23:00 | A | 2513.01000000 / 1.00080000 | 22:59:00 / 22:59:00 | 22:59:01 / 22:59:01 | 60.000 / 60.000 | 22:58:59.999000 / 22:58:59.988000 |
| 23:00 | B | 2513.01000000 / 1.00080000 | 22:59:00 / 22:59:00 | 22:59:01 / 22:59:01 | 60.000 / 60.000 | 22:58:59.999000 / 22:58:59.988000 |
| 23:00 | C | 2513.46 / 1.0009 | 22:59:58.817000 / 22:59:58.527000 | 22:59:59.817000 / 22:59:59.527000 | 1.183 / 1.473 | 22:59:58.817000 / 22:59:58.527000 |

At hourly decisions A/B cannot use the candle completing at the decision because its +1s availability is still in the future; their selected completion is 60s old. C selects an ETH trade at least 1s old and a USDC trade observed no later than that ETH trade and available by the ETH quote's availability. Historical markout endpoints instead allow endpoint quotes received by the later decision, while keeping observation time at or before each fixed endpoint.

For each decision below, endpoints belong to the same first mature Swap across A/B/C. Each endpoint lists the fixed target time and selected reference price (USDC/ETH); the 00:00 example falls on Jan 14.

| Decision UTC | Variant | Initial target UTC / price | Five-minute target UTC / price | Initial leg ages (ETH / USDC, s) | Target leg ages (s) |
|---|---|---|---|---|---|
| 00:00 | A | 2024-01-14T23:00:11+00:00 / 2507.586206896551724137931034 | 2024-01-14T23:05:11+00:00 / 2501.929228308676529388244702 | 11.000 / 11.000 | 11.000 / 11.000 |
| 00:00 | B | 2024-01-14T23:00:11+00:00 / 2507.586206896551724137931034 | 2024-01-14T23:05:11+00:00 / 2501.929228308676529388244702 | 11.000 / 11.000 | 11.000 / 11.000 |
| 00:00 | C | 2024-01-14T23:00:11+00:00 / 2506.6366816591703 | 2024-01-14T23:05:11+00:00 / 2501.439424230308 | 0.300 / 0.378 | 0.121 / 0.945 |
| 12:00 | A | 2024-01-15T11:00:11+00:00 / 2533.699780131920847491505097 | 2024-01-15T11:05:11+00:00 / 2531.697811531927650644548816 | 11.000 / 11.000 | 11.000 / 11.000 |
| 12:00 | B | 2024-01-15T11:00:11+00:00 / 2533.699780131920847491505097 | 2024-01-15T11:05:11+00:00 / 2531.697811531927650644548816 | 11.000 / 11.000 | 11.000 / 11.000 |
| 12:00 | C | 2024-01-15T11:00:11+00:00 / 2534.146097731588 | 2024-01-15T11:05:11+00:00 / 2531.6778255221343 | 0.634 / 1.483 | 0.123 / 0.446 |
| 23:00 | A | 2024-01-15T22:00:47+00:00 / 2520.365743979214549815129409 | 2024-01-15T22:05:47+00:00 / 2522.184470870390726491455981 | 47.000 / 47.000 | 47.000 / 47.000 |
| 23:00 | B | 2024-01-15T22:00:47+00:00 / 2520.365743979214549815129409 | 2024-01-15T22:05:47+00:00 / 2522.184470870390726491455981 | 47.000 / 47.000 | 47.000 / 47.000 |
| 23:00 | C | 2024-01-15T22:00:47+00:00 / 2520.27378097522 | 2024-01-15T22:05:47+00:00 / 2522.142286171063 | 1.240 / 1.717 | 0.041 / 0.561 |

The observations change both ETH and USDC prices. The identity `P_C/P_B = (ETH_C/ETH_B)/(USDC_C/USDC_B)` explains their arithmetic conversion: at 12:00 ETHUSDT rises from 2539.21 to 2539.36, but USDCUSDT rises from 1.0005 to 1.0006, so the converted reference falls. This is evidence of conversion influence, not an independent economic causal decomposition.

Within the original comparison window, 4 minutes end with USDC after ETH; 1 changes the final converted price. The saved witnesses show why closing already-converted ETH ticks would be a different construction.
At 2024-01-15T03:51:00+00:00, ETH closes at 2024-01-15T03:51:59.997000+00:00; USDC changes from 1.0006 to 1.00050000 at 2024-01-15T03:51:59.998000+00:00. Last-converted-tick versus separately closed legs: -0.999400360 bps.

C stores ratios as floats; the largest observed absolute Decimal-versus-stored ratio residual is 4.53018792483E-13 USDC/ETH. This residual is retained, without a new tolerance. Exact A/B close differences are counted above.

## Finding and limits

No implementation error was found in the checked minute construction, conversion or endpoint assignment. Under the same definition A and B agree exactly. Differences to C remain documented measurement sensitivity to aggregation and asynchronous observation selection.

The cache supports the displayed price/time arithmetic, but not a unique split between market movement, aggregation and currency effects, or actual historical reception latency. Trade prices are not midprices; the finer variant is not treated as ground truth for toxicity.
The five-minute markout remains signed reference-price movement, without DEX execution price or fees. No windows, delays or thresholds were tuned. Production references, fees, execution, costs and primary targets were not changed.

The figure's common-sample values, Swap identities and exact weights are in `data/validation/reference_resolution/common_markouts.parquet`. Other comparisons are recomputed from the cache.

# Feature measurement checks

Status: **WARN**. Measurements under stated availability assumptions; no predictive or profitability claim.

Fixed comparison day: **2024-01-15 UTC**, all 24 hourly decisions. History starts 2024-01-14T22:55:00+00:00; selected reference data end 2024-01-15T23:01:00+00:00 (exclusive).
Only block reception changes in the delay comparison: 12s versus 60s. Reference delay stays 1s; both maximum ages stay 120s.

## Temporal correction and full pilot

The saved real run has 48 feature rows, 48 satisfying required pool/reference joins, 144 scenarios and 144 sensitivity targets. Primary Y: 0. Overall pilot status: **WARN**.
Block observation time, last state change and assumed availability are separate. Original events, integer replay and fee calculations remain unchanged.

| Full-pilot feature | Available decisions |
|---|---:|
| absolute_dex_cex_deviation_bps | 48/48 |
| active_liquidity | 48/48 |
| dex_cex_deviation_bps | 48/48 |
| gas_price_mean_gwei | 48/48 |
| historical_markout_5m_bps | 48/48 |
| realized_volatility | 48/48 |
| swap_count | 48/48 |
| swap_volume_usdc | 48/48 |
| trailing_return | 48/48 |
| volume_imbalance | 48/48 |

## Minute versus aggregate trades

Fine-data status: **WARN**.
Minute measurements agree with the production feature builder: PASS (72 comparisons).

Fine quote conversion: 634531/634553 selected ETH trades; 22 lack a prior USDC quote, 0 have a stale conversion and 0 have an unavailable conversion. 623441 valid quotes have different leg timestamps. These quote exclusions are separate from the decision-level exclusions below.

| Block delay | Metric | Common decisions | Excluded | Median absolute difference (bps) | 95% absolute difference (bps) | Sign agreement |
|---|---|---:|---:|---:|---:|---:|
| baseline | dex_cex_deviation_bps | 24 | 0 | 3.23261 | 10.7387 | 14/24 |
| baseline | absolute_dex_cex_deviation_bps | 24 | 0 | 1.53413 | 5.81699 | 24/24 |
| baseline | historical_markout_5m_bps | 24 | 0 | 3.98458 | 7.80934 | 9/24 |
| delay_60s | dex_cex_deviation_bps | 24 | 0 | 3.23273 | 10.7387 | 15/24 |
| delay_60s | absolute_dex_cex_deviation_bps | 24 | 0 | 1.59432 | 7.69851 | 24/24 |
| delay_60s | historical_markout_5m_bps | 24 | 0 | 3.98458 | 7.80934 | 9/24 |

Differences are measured on decisions where both values exist. Zero is a distinct sign; quantiles use linear interpolation.
Each historical markout averages its valid mature swaps. Quote gaps can change trade membership even when both decision aggregates exist; per-variant counters below disclose that difference.

| Variant | Valid deviation | Valid historical markout | Mature swaps across windows | Immature swaps | Missing/invalid endpoints |
|---|---:|---:|---:|---:|---:|
| minute_baseline | 24 | 24 | 4527 | 363 | 0 |
| minute_delay_60s | 24 | 24 | 4527 | 311 | 0 |
| fine_baseline | 24 | 24 | 4527 | 363 | 0 |
| fine_delay_60s | 24 | 24 | 4527 | 311 | 0 |

Common-trade check (baseline): identical valid membership **True**; 4527 common mature Swaps, 0 minute-only and 0 fine-only. Reweighted common-trade median / 95% absolute differences: 3.98458 / 7.80934 bps; sign agreement 9/24 decisions.

Common-trade check (delay_60s): identical valid membership **True**; 4527 common mature Swaps, 0 minute-only and 0 fine-only. Reweighted common-trade median / 95% absolute differences: 3.98458 / 7.80934 bps; sign agreement 9/24 decisions.

Common-trade recombination uses original quote-volume weights and the production per-trade markout; its numerical comparison tolerance is 1e-9 bps.

Sign agreement for absolute deviations is mechanically nonnegative; the signed deviation and markout rows carry directional information.

## Delay sensitivity

| Reference | Metric | Common decisions | Median / 95% absolute difference (bps) | Sign agreement |
|---|---|---:|---|---|
| minute | dex_cex_deviation_bps | 24 | 0.119961 / 4.38524 | 23/24 |
| minute | absolute_dex_cex_deviation_bps | 24 | 0.119961 / 3.61344 | 24/24 |
| minute | historical_markout_5m_bps | 24 | 0 / 0 | 24/24 |
| fine | dex_cex_deviation_bps | 24 | 0.119911 / 4.3857 | 24/24 |
| fine | absolute_dex_cex_deviation_bps | 24 | 0.119911 / 4.3857 | 24/24 |
| fine | historical_markout_5m_bps | 24 | 0 / 0 | 24/24 |

## Fixed examples

Examples were fixed at 00:00, 12:00 and 23:00 UTC. Currency-leg prices and times are examined in [reference_resolution.md](reference_resolution.md).

| UTC decision | Minute / fine deviation (bps) | Minute / fine markout (bps) | Pool observed / last changed |
|---|---|---|---|
| 2024-01-15T00:00:00+00:00 | -6.27913 / 2.15639 | 11.3734 / 3.58279 | 2024-01-14T23:59:47+00:00 / 2024-01-14T23:59:47+00:00 |
| 2024-01-15T12:00:00+00:00 | 0.96461 / 1.37339 | 1.24356 / -4.0215 | 2024-01-15T11:59:11+00:00 / 2024-01-15T11:59:11+00:00 |
| 2024-01-15T23:00:00+00:00 | -0.607924 / -1.39921 | 9.86894 / 2.22686 | 2024-01-15T22:59:35+00:00 / 2024-01-15T22:59:35+00:00 |

Endpoints stay anchored to trade time and trade time +300s; a later price before the decision cannot replace them.

## Evidence and limits

A Swap at 12:00:20 targets the reference price at 12:05:20. With a one-second reference delay, its historical markout cannot enter a decision before 12:05:21, and both required quotes must actually have arrived. A 12:05:40 price cannot replace that target.
Historical aggregate trade prices are not order-book midprices. Both currency legs are observed; asynchronous legs are flagged and stale conversion quotes excluded. Retrieval times establish provenance, not historical information arrival.
The sampled gas warning remains in the full feature output. Position fees, operation-cost calibration and primary targets remain open. Agreement between references establishes neither true toxicity nor forecasting value.
Definitions: [feature_definitions.md](../docs/feature_definitions.md). The comparison is reproducible from the cached minute and aggTrade archives.

## Pool-time example

At 2024-01-15T07:00:00+00:00, the relevant state last changed at 2024-01-15T06:57:47+00:00. Header block 19010653 confirms it through 2024-01-15T06:59:47+00:00; assumed availability is 2024-01-15T06:59:59+00:00. The observation is 13.0s old, although the last change is 133.0s old. The 120s observation limit is unchanged.

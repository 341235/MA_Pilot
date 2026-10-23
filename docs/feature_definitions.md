# Feature definitions

These are measurements available at a decision, not validated predictors.
Imbalance and DEX–CEX deviation are candidate proxies for order flow or toxicity;
they do not identify informed traders. The markout is reference-price drift,
not exact LVR, execution profit or a position's realized return.

## Prices, signs and timing

All prices below are **USDC per ETH**. The default reference is
`P_ref = ETHUSDT / USDCUSDT`, using actual Binance prices for both legs.
Neither USDT/USDC parity nor a missing conversion quote is assumed. Minute
observations are completed candle closes, not order-book midprices. The finer
comparison uses public historical trade prices, also not midprices.

The pool price is `P_pool = 2**192 * 10**12 / sqrtPriceX96**2` because token0
is USDC with 6 decimals and token1 is WETH with 18 decimals. It is a marginal
pool price before the 0.05% swap fee, not a finite-size execution quote.

For Swap `i`, `v_i = abs(amount0_i) / 10**6` is USDC quote volume and
`s_i = +1` when `amount1_i < 0`: the taker receives WETH and buys ETH.
`s_i = -1` when `amount1_i > 0`: the taker sells ETH. Amount signs are the
pool's balance changes; USDC input volume includes the amount paid to the pool.
A positive quote volume with zero WETH change has no classified direction.

Defaults are lookback `L=3600s`, markout horizon `h=300s`, block reception delay
`d_b=12s` and reference delay `d_r=1s`. Delays are explicit assumptions about
historical information availability, not the later download time. A supplied
later `available_at` is retained. No future interpolation or normalization is used.

An observation may enter a decision at `t` only when its timestamp and
`available_at` are both at most `t`. A close for the minute beginning `u`
is timestamped `u+60` and becomes available no earlier than `u+60+d_r`.
Both conversion legs must satisfy the reference freshness limit, 120 seconds;
their observation times, ages and granularity are kept for the finer comparison.

Pool freshness uses a real header's observation time `o`, not its last state
change. The state is confirmed only after every pool event through that block
has been processed under verified complete log coverage. Missing empty-block
headers create no observations; a missing event-block header prevents confirmation.
At a decision require `o+d_b <= t` and `t-o <= 120s`. Actual later availability
can make the first condition stricter. Increasing delay cannot reduce `t-o`.
Original event times and the last relevant change remain recorded separately.
The initial snapshot anchors a known state; its preceding change time is unknown.

## Market features

| Field | Formula and unit | Source and window |
| --- | --- | --- |
| `trailing_return` | `P_ref(t) / P_ref(t-L) - 1`, dimensionless | Backward reference endpoints at `t` and `t-L` |
| `realized_volatility` | `sqrt(sum(log(P_j/P_(j-1))**2))`, dimensionless, not annualized | Available completed reference closes in `[t-L,t]` |
| `swap_volume_usdc` | `sum(v_i)`, USDC | Received pool Swaps with `t-L < t_i <= t` |
| `swap_count` | Number of those Swaps, integer | Same window and reception condition |
| `active_liquidity` | Pool's integer liquidity, decimal text | Latest fresh confirmed block state |

Each return endpoint uses a backward quote available **at that endpoint**, not
one that arrived later before the decision. Missing or stale endpoints give a
null return. Volatility uses at least two prices, consecutive gaps at most 120s,
and first/last quotes no more than 120s from the window boundaries; otherwise it
is null with `INCOMPLETE_VOLATILITY_WINDOW`. A flat covered window gives zero.

Swap earliest availability is its block time plus `d_b`, or a later explicit
arrival time. Block time does not resolve individual transaction arrival times.
Events on the open left boundary are excluded. A verified window
without received Swaps gives volume and count zero. Unavailable on-chain
coverage gives null values with `ONCHAIN_UNAVAILABLE`. Confirmed quiet blocks
refresh observation evidence but contribute neither volume nor synthetic Swaps.
Active liquidity is not token inventory or TVL; missing or stale pool evidence
leaves it null. Fees and the LP's hypothetical position do not enter these features.

## Order flow and price differences

| Field | Formula and unit | Source and window |
| --- | --- | --- |
| `volume_imbalance` | `sum(s_i*v_i) / sum(v_i)`, dimensionless in `[-1,1]` | Same available Swap window as volume |
| `dex_cex_deviation_bps` | `10000*(P_pool(t)/P_ref(t)-1)`, basis points | Fresh, available pool and reference observations at `t` |
| `absolute_dex_cex_deviation_bps` | Absolute signed deviation, basis points | Same observations |

Positive imbalance means more USDC quote volume from ETH buyers. No volume
gives null, not zero imbalance. A positive-volume Swap with zero `amount1`
makes imbalance null with `UNCLASSIFIED_SWAP_DIRECTION`; its volume and count
remain observed. No such WETH-zero Swap occurs in this pilot's real extraction.
Missing coverage also leaves imbalance null. Signed deviation is positive when
the pool price exceeds the reference. Missing or stale inputs leave both
deviations null; transaction fees, slippage and conversion execution costs are
not deducted. The observation ages can differ and are retained in the outputs.

## Historical five-minute markout

For a received Swap at `t_i`, define the fixed target `q_i=t_i+h` and
`m_i = 10000*s_i*(P_ref(q_i)/P_ref(t_i)-1)` in basis points.
`historical_markout_5m_bps = sum(v_i*m_i) / sum(v_i)` over valid mature Swaps
with `t-L < t_i <= t`. The window is anchored to trade time, not completion time.
Both reference prices are backward quotes anchored to their respective target
times; each must be fresh at its target and available by the decision.
A price after `q_i` cannot replace a missing quote at `q_i`.

Earliest eligibility is the latest of Swap availability, `q_i+d_r`, and the
actual availability of both endpoint quotes. Thus a Swap at 12:00:20 with
`h=300s` first matures at 12:05:21 under the one-second reference delay,
provided its required endpoint prices have arrived. A 12:05:40 quote cannot
stand in for the 12:05:20 target, even at a 12:06 decision.

Positive markout means a favorable subsequent reference move for the ETH taker.
The formula uses neither pool execution price nor paid swap fees. It excludes
immature trades, missing/stale/invalid endpoints, zero quote volume and undefined
direction. Outputs retain `markout_mature_count`, `markout_immature_count` and
`markout_missing_reference_count`. The last counter also covers invalid volume,
direction or price, despite its historical name. No usable mature trades give
null with `NO_MATURE_MARKOUTS`; exclusions add `MARKOUT_REFERENCE_GAPS`.

## Gas and interpretation

`gas_price_mean_gwei` is the arithmetic mean of `effectiveGasPrice/10**9` over
unique received Swap transactions with sampled receipts in `(t-L,t]`.
`gas_transaction_count` counts those hashes once, even if a transaction has
multiple Swaps. These are gas prices in gwei, not `gasUsed` or operation costs.
The historical receipt is treated as available with its block under `d_b`.
Absent sampled receipts leave the mean null; with verified coverage the count
is zero. Missing on-chain coverage leaves the count null.

Real sampled means keep `GAS_RECEIPTS_SAMPLED_NOT_POPULATION_MEAN` and do not
estimate all pool transactions. Receipt selection prioritizes a Swap available
before each decision, then LP operations, deduplicated by transaction hash.
This sample does not calibrate Mint/Burn/Collect
costs or remove their attribution limitations. Market-feature measurement is
checked separately from position fees, empirical costs and primary performance
targets. See [feature validation](../reports/feature_validation.md) for executed
checks and [data contract](data_contract.md) for source evidence.

The fixed-day [reference-resolution diagnosis](../reports/reference_resolution.md)
reconstructs each currency leg's minute close from the existing aggTrades cache
and compares identical definitions on common decisions and Swap samples.

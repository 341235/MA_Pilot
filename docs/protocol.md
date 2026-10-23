# Protocol notes

The pilot reconstructs pool state and estimates fees for hypothetical LP ranges.
Synthetic fixtures check arithmetic and accounting; historical validity also
requires the snapshot, log coverage, reference prices and external checks.

## Arithmetic and upstream code

The reference is [Uniswap v3-core v1.0.0](https://github.com/Uniswap/v3-core/tree/v1.0.0), inspected on 2026-10-22.

| Source | Used for |
| --- | --- |
| [TickMath.sol](https://github.com/Uniswap/v3-core/blob/v1.0.0/contracts/libraries/TickMath.sol) | Integer multipliers, tick limits and Q64.96 rounding. The inverse uses binary search over the forward function rather than the assembly logarithm. |
| [SqrtPriceMath.sol](https://github.com/Uniswap/v3-core/blob/v1.0.0/contracts/libraries/SqrtPriceMath.sol) | Token deltas and directional rounding, including the token0 overflow fallback. |
| [SwapMath.sol](https://github.com/Uniswap/v3-core/blob/v1.0.0/contracts/libraries/SwapMath.sol) | Exact-input/output steps, fees and final exact-output cap. |
| [TickBitmap.sol](https://github.com/Uniswap/v3-core/blob/v1.0.0/contracts/libraries/TickBitmap.sol), [UniswapV3Pool.sol](https://github.com/Uniswap/v3-core/blob/v1.0.0/contracts/UniswapV3Pool.sol) | Word boundaries, crossings, fee growth, Flash payments and protocol cuts. |
| [TickMath](https://github.com/Uniswap/v3-core/blob/v1.0.0/test/TickMath.spec.ts), [SqrtPriceMath](https://github.com/Uniswap/v3-core/blob/v1.0.0/test/SqrtPriceMath.spec.ts), [SwapMath](https://github.com/Uniswap/v3-core/blob/v1.0.0/test/SwapMath.spec.ts) tests | Numeric regression vectors, including a one-unit exact-output cap and tiny input consumed entirely as fees. |

The release tag is pinned, but its commit hash and deployed runtime-bytecode
equivalence were not independently checked. Tests execute Python, not Solidity/EVM.
Upstream copyright is 2021 Uniswap Labs. TickMath uses GPL-2.0-or-later; the other
files have BUSL-1.1 headers. The [upstream license](https://github.com/Uniswap/v3-core/blob/v1.0.0/LICENSE)
sets a change date no later than 2023-04-01 and GPL v2 or later as the change
license. Keep attribution, SPDX notices and source licenses on redistribution.

Raw values use Python integers; float snapshot/event integers are rejected.
Decimal strings are accepted at JSON/CSV boundaries. `mul_div` permits a 512-bit
intermediate product, with explicit Solidity result bounds. TickMath, price
changes, deltas, protocol fees and fee-growth increments do not use floats.

## Prices and positions

`sqrt_price_x96 / 2**96` is the square root of raw token1/token0 price:

```
P_1_per_0 = (sqrt_price_x96 / 2**96)**2 * 10**(decimals0 - decimals1)
```

For this pool, token0=USDC (6 decimals), token1=WETH (18); USDC/WETH is the
reciprocal. The access layer verifies identity; `PoolState` alone does not.
`tick_bounds_for_price` uses `center/(1+w)` and `center*(1+w)`, converts to raw
orientation, then rounds lower/upper ticks outward to spacing multiples.
Inversion reverses the economic endpoints. Out-of-bounds ranges fail.

For lower/current/upper Q64.96 sqrt prices `a <= s <= b`:

```
amount0 = L * (b-s) * 2**96 / (b*s)
amount1 = L * (s-a) / 2**96
```

Clamp `s` to `[a,b]`: below range only token0, above range only token1. Mint
deposits round up; principal reports round down. `liquidity_for_capital` finds
the largest integer liquidity affordable at an external WETH/USDC reference.
It assumes this token order and excludes trades to acquire starting inventory.

## Initial state

The snapshot describes the end of the block immediately before replay starts.
The extraction manifest must establish continuity; an earlier block alone does
not prove it. Warm-up data cannot replace the complete initial tick state.

| Required field | Meaning |
| --- | --- |
| `block_number`, `block_hash`, `timestamp`, `provenance` | Canonical block, UTC seconds and actual source; synthetic fixtures are labelled. |
| `sqrt_price_x96`, `tick`, `liquidity` | Slot0 state and active uint128 liquidity. |
| `fee_pips`, `tick_spacing` | Verified pool immutables. |
| `fee_protocol0`, `fee_protocol1` | Denominators, each 0 or 4..10; packed uint8 `fee_protocol` is also accepted. |
| `ticks_complete: true`, `ticks` | Complete initialized `{tick, liquidity_gross, liquidity_net}` entries, supported by extraction evidence. |

Optional global and tick-outside fee-growth fields enable further comparisons.
Missing growth stays `None`. New ticks follow initialization rules when global
growth is known; crossings use modulo `2**256`, and cleared ticks are removed.
Checks cover spacing, bounds, gross >= abs(net), per-tick liquidity bounds, zero
sum of net liquidity, and active liquidity from net ticks at/below the current
tick. Downward boundary crossings may leave `slot0.tick = mathematical_tick-1`.
These checks establish consistency, not provenance. Active liquidity is not TVL.

## Events and replay

Use verified pool logs only; position-manager logs would duplicate activity.
Each event needs block/hash/time, transaction hash/index, log index and ABI fields.
Order is `(block_number, transaction_index, log_index)`. Identical duplicates
warn and are removed. Conflicts, removed logs, reorg hashes and inconsistent
positions/times fail. Count/hash coverage is checked upstream.

- `Mint`/`Burn`: update lower/upper net and gross liquidity and active liquidity
  when in range. A zero Burn poke creates no fees; Burn token amounts are principal.
- `Swap`: use signed amounts and final price/tick/liquidity. Cross initialized
  ticks and uninitialized bitmap word edges; both affect reference fee rounding.
- `Flash`: fees use required `paid0/paid1`, including donations after principal
  repayment, with historical protocol cuts. Borrowed amounts are not fee income.
- `SetFeeProtocol`: validate new denominators and supplied old values.
- `Collect`, `CollectProtocol`, `IncreaseObservationCardinalityNext`: no new LP
  fees. Collect may combine burned principal and fees. Unknown event types fail.

Replay does not reconstruct pool balances, owner settlements, oracle accumulators or TVL.
With `amount_specified` (positive exact input, negative exact output) and a
directional `sqrt_price_limit_x96`, `simulate_swap` checks both signed amounts
and all final state values using the integer reference loop
(`reference_exact`). Callbacks, token behavior, traces and oracle state remain
outside this check. Router/multicall inputs may require decoded internal calls.

Event-only replay splits the observed path at ticks and word edges. Intermediate
fees use upward reference rounding; the final residual must match normal rounding
or reproduce the endpoint through a partial exact-input step. Arbitrary residuals
fail. An implied output exactly one raw unit above the emitted amount is allowed
with `endpoint_output_rounding: WARN` for the final exact-output cap. Other output
differences fail; price, tick and liquidity always have zero integer tolerance.

This route is `event_endpoint_approximation` / `WARN`. Independent checkpoints
need separately sourced evidence; an event compared with itself is insufficient.
The pipeline checks block identity, price, tick, liquidity and available
global fee growth exactly, but not tick-outside histories or position settlement.

## Marginal fees and limits

For historical active liquidity `L_h`, hypothetical liquidity `L_p` and the
historical fee after protocol cut `F_lp`:

```
F_position = F_lp * alpha * L_p / L_h
```

`alpha` is 1 inside the range, 0 outside, otherwise the in-range input delta over
the whole segment's input delta. Deltas round up; Decimal allocation may contain
fractional raw units. Clipping inserts hypothetical boundaries without changing
historical execution. Unchanged-price fees and Flash use lower-inclusive,
upper-exclusive tick membership. Fees stay separate by token without reinvestment.

This marginal approximation always remains `WARN`: adding an actual LP changes
liquidity, integer growth flooring and settlement. Collect/principal are excluded.
Perturbation is `max(L_p/L_h)` without adding `L_p` to the denominator. Active
hypothetical ranges with zero historical liquidity block fees. An empty segment
list yields conditional zero with a coverage warning; missing evidence gives `None`.
Global checkpoints test total accrual. Range validation also needs relevant
outside-growth histories, including tick initialization/clearing between endpoints.

Arithmetic vectors, enriched event amounts, reconstructed state and known growth
require exact integers (modulo uint256 where defined). The one-unit output case
above and marginal allocation retain WARN. Missing historical initialization or
coverage is BLOCKED. Synthetic PASS does not resolve these historical gates.

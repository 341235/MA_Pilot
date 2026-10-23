# Historical position-fee check

Overall **WARN**. This checks a small historical sample, not all pilot fees.

Pool `0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640`. End-of-block interval **(19012917, 19017934]**, 2024-01-15T14:36:35+00:00 to 2024-01-16T07:23:47+00:00. All events after the start block through the complete end block are included.

## Selection and evidence

First consecutive independent checkpoint pair with both timestamps inside the configured pilot. CORE identities observed in complete logs with positive net observed Mint-minus-Burn through start; no own Mint/Burn (including zero)/Collect in (start,end]. First lexicographic(owner,lower,upper) per path class, before evaluating fees.

Eligible structural cases: {'active': 26, 'crossing': 6, 'inactive': 2}. Selection was frozen before RPC retrieval and fee comparisons in `data/validation/position_fees/selection_plan.json`.

All selected owners are `0xc36442b4a4522e871399cd717abdd847ab11fe88` (the Position Manager). Identification is the **Core owner/tick-range aggregate**, not an individual NFT. No NFT states were used.

Existing raw checkpoint calls supply both endpoint ticks and global growth. Only the missing boundary/position reads were added: 18 `eth_call`, 2 HTTP batches, 8768 bytes, 468 estimated CU. All reads use the checkpoint's canonical block hash. These are separate state queries from the same provider as the logs, not a second-provider confirmation.

Complete interval events: {'Swap': 3427, 'Mint': 47, 'Burn': 50, 'Collect': 47}; 3700 fee segments. There are 14 zero-amount Burns elsewhere, none on a selected identity. Flash and SetFeeProtocol counts are both zero. All selected identities have no Mint/Burn/poke/Collect; liquidity and all stored position fields are unchanged. Every intervening boundary Mint/Burn is checked: no boundary clears or reinitializes, and gross liquidity exactly reaches the independent end snapshot.

| Case | Range [lower, upper) | Existing liquidity | Crossings | Maximum historical liquidity share | Status |
|---|---|---:|---:|---:|---|
| active | [188520, 207240) | 106757385633941 | 0 | 0.001479% | PASS |
| crossing | [197610, 198050) | 1205026156682482170 | 14 | 14.322094% | PASS |
| inactive | [197310, 197880) | 14803226942591558 | 0 | 0.000000% | PASS |

## Comparison rules and raw token results

For each token independently, `inside = global - below - above (mod 2^256)`, with lower inclusive and upper exclusive. B uses separate RPC endpoint states and the independent reference helper. Exact checks require `deltaInside = sum(floor(lpFee*Q128/historicalL)) (mod 2^256)` on whole active segments and `P = floor(positionL*deltaInside/Q128)` with zero integer tolerance.

A is the unchanged production `marginal_fees(segments, lower, upper, existingL)` function. The existing position is already in historical liquidity: nothing is added to the denominator. A preserves fractional raw units. For the exact rational ideal `E=sum(lpFee*existingL/historicalL)`, the predeclared rounding rule is `0 <= E-P < 1+n*existingL/Q128`. Decimal representation error is checked separately from this protocol truncation, using the fixed 90-digit arithmetic bound in `arithmetic_rules.json`, never a fitted tolerance.

| Case | Token | B state claim (raw) | Event protocol claim (raw) | Exact difference | A marginal (raw, display rounded) | A minus B (raw, display rounded) |
|---|---|---:|---:|---:|---:|---:|
| active | USDC | 212054 | 212054 | 0 | 212054.350295736 | 0.350295736 |
| active | WETH | 82574044503870 | 82574044503870 | 0 | 82574044503870.969603740 | 0.969603740 |
| crossing | USDC | 2208570922 | 2208570922 | 0 | 2208570922.202025057 | 0.202025057 |
| crossing | WETH | 858179572321846010 | 858179572321846010 | 0 | 858179572321846010.593383664 | 0.593383664 |
| inactive | USDC | 0 | 0 | 0 | 0.000000000 | 0.000000000 |
| inactive | WETH | 0 | 0 | 0 | 0.000000000 | 0.000000000 |

Display rounding above changes no comparison. The validator recomputes growth, unrounded amounts and rounding/precision bounds from the cached endpoint responses. USDC raw units are 1e-6 USDC; WETH raw units are 1e-18 WETH. Token discrepancies are never netted against each other.

## Accrual, stored owed and transfers

The interval claim isolates new growth; it is not the change in stored `tokensOwed`. Swaps do not settle the position storage. Pending fees at each endpoint use that position's actual stored lastInside. A prior fractional remainder can make their difference one raw unit larger than an interval-only floor. All sampled claims fit uint128; the reference reports full accrual before that storage cast.

| Case | Stored tokensOwed0 (both endpoints) | Stored tokensOwed1 (both endpoints) | Collected 0 / 1 | Returned principal 0 / 1 |
|---|---:|---:|---|---|
| active | 0 | 0 | 0 / 0 | 0 / 0 |
| crossing | 0 | 0 | 0 / 0 | 0 / 0 |
| inactive | 2819936 | 1221669288988695 | 0 / 0 | 0 / 0 |

Actual carry example: active WETH pending claim changes from 185187896376955 to 267761940880826, a change of 82574044503871; isolated interval claim is 82574044503870. Difference: 1 raw unit, inherited from the prior fractional remainder.

## Finding and remaining limits

All six token/range protocol comparisons pass exactly. Production's fractional differences are explained by the separately proved growth and settlement rounding rules. No existing fee implementation error was found in these three historical cases.

The in-range case earns both tokens; the crossing case covers 14 crossings of its upper boundary; the inactive control earns zero even at its upper-exclusive boundary. Stored owed amounts on that inactive position originate before the interval and are not new earnings. No transfers occurred on any sampled identity, so actual collection settlement and changing-liquidity cases remain untested here.

This does not validate counterfactual ranges with historically uninitialized boundaries or partial-segment fee interpolation. The large existing crossing aggregate also does not validate the pilot's small hypothetical LP assumption. The unchanged historical price path remains the pilot's marginal assumption. Fees, costs, features and reference prices are unchanged; primary targets remain null and fee assurance remains WARN globally.

Official reference actually checked: v3-core **v1.0.0**, commit `e3589b192d0be27e100cd0daaf6c97204fdb1899`: [Tick.sol](https://github.com/Uniswap/v3-core/blob/e3589b192d0be27e100cd0daaf6c97204fdb1899/contracts/libraries/Tick.sol), [Position.sol](https://github.com/Uniswap/v3-core/blob/e3589b192d0be27e100cd0daaf6c97204fdb1899/contracts/libraries/Position.sol), [pool state interface](https://github.com/Uniswap/v3-core/blob/e3589b192d0be27e100cd0daaf6c97204fdb1899/contracts/interfaces/pool/IUniswapV3PoolState.sol), and the pinned pool implementation. Full sources and hashes are in `data/validation/position_fees/upstream/manifest.json`; this is not a deployed-bytecode equivalence proof.

RPC responses, fixed rules and sources: `data/validation/position_fees/`.

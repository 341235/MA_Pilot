# Pilot results

Mode: **real**; scope: **full**; status: **WARN**.
End-to-end validation: **NOT ESTABLISHED**. 48 decisions and 144 fixed-range scenarios.

## Data

Decisions: 2024-01-15T00:00:00Z to 2024-01-17T00:00:00Z (exclusive).
Extraction: 2024-01-14T00:00:00+00:00 to 2024-01-17T01:05:24+00:00 (exclusive), including 24h warmup and the outcome tail.
On-chain source: Ethereum RPC. Development data, not a final holdout.

Rows: events 14,300; states 14,301; swap_segments 15,100; transaction_costs 100; pool_observations 10,045; reference_prices 4,385; features 48; outcomes 144.
Reference source: Binance closed 1m ETHUSDT / USDCUSDT, with publisher checksums.
Minute coverage: 4385 observations, 0 missing boundaries; longest gap 0 minutes.

## Checks

| Check | Status | Detail |
|---|---|---|
| reference_import | PASS | 4385 converted closed-minute reference observations; no USDC=USDT assumption |
| coverage | PASS | Contiguous RPC block intervals, raw-log artifacts, counts, headers and checksum verified; reference coverage complete |
| pool_identity | PASS | Saved RPC immutable and token-decimal calls match configured pool; historical access is separate |
| initialization | PASS | Full historical bitmap/tick state independently decoded from hash-pinned RPC artifacts at block 19001407; snapshot checksum and liquidity invariants verified; same-provider evidence |
| replay_initial_tick_distribution | PASS | Complete supplied tick map has consistent net and active liquidity; provenance requires independent verification. |
| replay_swap_event_consistency | WARN | 13617 endpoint-conditioned swaps. Event comparisons are internal consistency, never independent checkpoints. |
| reconstruction | PASS | 4 separately called historical checkpoints: exact price, tick, active liquidity and both global fee-growth comparisons; same RPC provider, independent method from event replay; integer tolerance=0 |
| pool_observations | WARN | 10045 evidenced end-of-block observations; complete intervening pool logs, assumed block reception delay |
| temporal_availability | WARN | 48/48 feature rows meet backward-join freshness rules; explicit reception assumptions; leakage tests are separate software evidence |
| position_fees | WARN | Marginal swap-segment fee approximation; endpoint call modes and position fee-growth settlement remain unvalidated |
| costs | WARN | Explicit gas/conversion sensitivity is complete; empirical primary costs unavailable |
| outcomes | WARN | 144/144 episodes have principal values; primary Y needs validated fees and empirical costs |

## Results

10/10 feature fields are available at every decision. Primary Y: **0/144**; sensitivity Y: **144/144**.
DEX/reference deviation (bps), min/median/max: -7.497369305727863 / 0.19356023926664356 / 9.287410478042712.
Hypothetical/historical liquidity share, min/median/max: 0.0007629545480335309 / 0.0022181509797356263 / 0.005596163887020706.
Market-price differences are not reconstruction errors; individual flags remain in the feature/outcome tables.

## Historical checkpoints

20/20 exact pool-state comparisons. Separate hash-pinned calls use the same provider as the logs; these check state and global fee growth, not arbitrary position fees.

| Block | Exact comparisons | Largest absolute difference |
|---|---:|---:|
| 19007707 | 5/5 | 0 |
| 19012917 | 5/5 | 0 |
| 19017934 | 5/5 | 0 |
| 19023214 | 5/5 | 0 |

## Assumptions and limits

- Reception delays: block 12s, reference 1s; execution delay 12s. Maximum ages: reference 120s, pool observation 120s.
- Pool observations require evidenced headers and complete intervening logs. Warmup does not replace historical initialization.
- Decision-time ranges stay fixed, with independent episode capital. Fees are held separately; WETH fees retain price exposure.
- Production fees remain a marginal segment approximation. The separate three-case position check does not validate all hypothetical ranges. Endpoint-conditioned swaps may carry a flagged one-unit output cap.
- Protocol state tolerance: 0; analytical relative tolerance: 1e-12; small-LP limit: 1.00%.
- Y uses left-side WETH principal on a 60s grid, a discrete residual rather than exact continuous LVR. Gas features use sampled receipts. Costs cover separate operations and initial conversion; terminal holdings are marked. Missing empirical costs keep primary Y null.

Related checks: [feature validation](feature_validation.md), [reference diagnosis](reference_resolution.md), [position fees](position_fee_validation.md).

Reproduce from cached inputs:

```sh
.venv/bin/python -m lp_pilot run --config configs/pilot.toml
```

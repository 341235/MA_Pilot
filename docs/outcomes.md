# Episode outcomes

Each decision independently receives 10,000 USDC. The three widths share the
same information and historical path; episodes do not form an equity curve.
This is a development sample.

## Range and holdings

The latest available decision-time pool price centers bounds
`P_center/(1+w)` and `P_center*(1+w)`, for `w={0.005,0.01,0.02}`. Ticks round
outward on the verified spacing grid. With USDC token0 and WETH token1, human
USDC/WETH is the decimal-adjusted reciprocal of raw token1/token0 price.
The range remains fixed after the decision.

Entry is `decision_time + execution_delay_seconds`; exit is one hour later.
Events at or before entry precede the hypothetical mint; fees accrue only for
`entry < event_timestamp <= exit`. This whole-block convention does not claim
observable transaction placement within a block.

The marginal LP leaves the historical path unchanged. Integer liquidity is the
largest whose rounded-up mint amounts cost at most capital at the available
reference price; withdrawal amounts round down. Unused funding stays as cash,
is reported and cancels from P&L. Costs are an additional charge, so the model
does not claim fully self-financing cash execution. Inventory is valued as
`V = x_USDC + x_WETH*P_ref`. Stale/missing state or reference prices block outcomes.
In-range fraction integrates `lower_tick <= tick < upper_tick` over time.

Fees stay in their received tokens, separate from principal and without
reinvestment. Segment allocation uses historical liquidity and range overlap;
see [protocol.md](protocol.md). The maximum `L_hyp/L_historical` is checked over
fee segments and every interval state, including Mint/Burn changes; the default
small-LP limit is 1%. Zero historical liquidity, a swap without fee segments,
unsupported fee events or invalid initialization block the affected calculation.
No segments means zero fees only when event coverage is complete.

The [historical position-fee check](../reports/position_fee_validation.md)
compares three existing Core aggregates against independent endpoint states.
It keeps fractional marginal fees separate from integer settlement and does not
remove the limitations for hypothetical boundaries or partial segments.

## P&L

`F_end = fee_USDC + fee_WETH*P_ref(exit)`

`Pi_LP = V_end_ex_fees + F_end - V_start_ex_fees - C`

`Pi_hold = initial_WETH*(P_ref(exit)-P_ref(entry))`

`P&L_vs_hold = Pi_LP - Pi_hold`

The rebalancing grid starts at entry, advances by `benchmark_seconds` (60 by
default), and includes exit. Principal WETH inventory `x_j` uses the latest
confirmed pool observation at or before the **left** endpoint of interval
`[t_j,t_(j+1)]`. Event times remain unchanged; quiet blocks only refresh the
evidence for an unchanged state:

`Pi_RB = sum_j x_j*(P_ref(t_(j+1))-P_ref(t_j))`

`Y = Pi_LP - Pi_RB`

Grid prices use available backward references with the maximum age; any missing
endpoint blocks the benchmark. Retrospective principal holdings do not expand
the decision information set. This residual is not exact continuous LVR, and no
extra LVR term is subtracted. WETH fees retain price exposure: their exit value
enters `F_end`, while the benchmark excludes fee inventories. Each row also
compares `Y` with the middle width under this convention.

## Costs and status

Sampled `transaction_costs.parquet` records `gasUsed*effectiveGasPrice` wei and
divides by `1e18` for ETH, once per transaction hash. Conflicting duplicates fail.
USDC valuation uses an available backward reference and maximum age; missing
prices remain null. Multicall gas is not allocated to individual events.

Empirical calibration accepts only reviewed single-operation, single-position
receipts. The hypothetical model prices mint at entry and burn/collect at exit;
even attributable medians model an unexecuted position. Initial conversion costs
are `capital*token_conversion_bps/10000`. Ending tokens are marked and retained,
without terminal liquidation. Approvals, aggregator fees and funding mechanics
need extra evidence before an investable-strategy interpretation.

Missing gas, price or conversion inputs give null costs. Configured constants
populate only `sensitivity_net_lp_pnl_usdc`, `sensitivity_pnl_vs_hold_usdc` and
`sensitivity_y_usdc`; primary `net_lp_pnl_usdc` and `y_usdc` stay null. CLI empirical
mode ignores those gas constants and requires attributable receipts plus separate
conversion evidence. A low-level empirical model must also supply conversion
costs; an explicit zero is accepted as an assumption.

All calculated outcomes remain at least `WARN` because fees and costs are modeled.
The full real run has 144/144 sensitivity targets and no blocked cases, using
state observations confirmed by actual block headers and a 120-second age limit.
Primary targets remain null. Passing state checkpoints or feature measurements
does not validate range fee settlement or empirical implementation costs.

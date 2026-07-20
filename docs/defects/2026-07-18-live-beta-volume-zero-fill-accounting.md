# WEEX-LIVE-001: Beta volume reported zero fills after real Maker execution

- Severity: High
- Affected surface: Live `weex live beta-volume` execution result
- First confirmed: 2026-07-18
- Status: Fixed in code; pending the next separately confirmed live validation

## Summary

A four-leg live Beta-volume workflow opened and closed BTC/ETH successfully. The executor returned `completed`, but its
result reported zero fills and zero quote volume. A separate read from WEEX `GET /capi/v3/userTrades` proved that the run
actually produced four Maker fills and non-zero turnover. Exact account-level amounts are intentionally omitted.

This was not only a display defect. The old completion rule could infer success from position movement while treating
`maker_only=true` as an empty assertion when no fill had been observed.

## Observed evidence

| Signal | Executor result | Authoritative WEEX fills |
| --- | ---: | ---: |
| Workflow status | completed | four open/close fills present |
| Fill count | 0 | 4 |
| Quote volume | 0 | non-zero (redacted) |
| Maker count | not independently proven | 4 |
| Taker count | not independently proven | 0 |
| Final BTC/ETH position | flat | flat |
| Remaining regular/trigger orders | none | none |

No credentials, raw account payloads, exchange order identifiers, account identifiers, or exact account-level amounts are
recorded in this report.

## Root cause

1. The live venue read terminal order state from regular order history.
2. WEEX/CCXT sometimes returned a terminal row without populated executed quantity or cumulative quote.
3. The adaptive executor correctly saw that the account position had reached its target, but its order observation still
   contained `filled_quantity=0` and `cumulative_quote=0`.
4. `LiveBetaVolumeService` copied those observation counters directly into its public result.
5. Completion required a flat final position, no open orders, and truthy per-leg `maker_only`. Since no fill event had been
   observed, `maker_only` remained at its optimistic initial value.

## Safety impact

- A successful run could under-report generated turnover.
- A terminal run could claim pure Maker without authoritative fill-side evidence.
- A web control plane could advance strategy progress from planned turnover rather than verified turnover.
- Operators could not distinguish an exchange fill-reporting delay from a genuine zero-fill result.

## Fix

The execution path now separates two data sources:

- Executor observation: queueing, submissions, cancels, timing, and sparse order-state diagnostics.
- Authoritative accounting: normalized WEEX `userTrades` rows filtered to the order identities submitted by that leg.

Each leg must now pass all of these checks before the next leg starts:

1. The fill query is complete.
2. At least one matching fill is visible.
3. Aggregated base quantity matches the leg quantity within half an amount step.
4. Every matching fill has `maker=true`.
5. No matching fill has unknown liquidity.
6. Aggregated `quoteQty` is positive.
7. No regular order remains for the symbol.

Final `completed` additionally requires four verified legs, BTC/ETH flat, no regular orders, and verified quote turnover at
or above the requested target. Read-only fill visibility checks are bounded; order submission is never retried.

## Regression coverage

- A sparse terminal order with zero executor fills is reconciled to non-zero authoritative fills.
- Taker and unknown-liquidity fills fail closed.
- Missing fills remain unverified after bounded read-only checks.
- Public results expose authoritative volume while retaining executor observations under diagnostics.
- Human output shows source, Maker/Taker counts, fees, PnL, per-leg verification, and final positions.

## Residual risk and validation

The next live run must be separately planned and confirmed. Acceptance requires public result totals to match an independent
`trades report` query for the same interval, with zero Taker volume and a flat/no-orders final account state.

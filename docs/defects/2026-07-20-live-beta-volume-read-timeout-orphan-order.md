# WEEX-LIVE-002: Read timeout left a live Maker order unmanaged

- Severity: Critical
- Affected surface: Live `weex live beta-campaign` / `beta-volume`
- Campaign: redacted
- Child plan: redacted
- First confirmed: 2026-07-20
- Status: Fixed in code; pending a separately confirmed live validation

## Summary

During the first opening phase, BTC opened successfully and ETH submitted a replacement `POST_ONLY` order. A read-only
position request then timed out. The exception escaped the adaptive executor and the coordinator classified every escaped
exception as `submission_uncertain`, even though the ETH submission had already returned a known order identity.

That classification disabled further work on the ETH lane without first canceling its known active order. The order filled
about 6.7 seconds after the exception. A separate BTC position read then timed out at the closing barrier, so BTC was not
flattened either. The campaign stopped with two positions and no remaining open orders.

The account was subsequently recovered with separately confirmed pure-Maker closes. BTC/ETH positions, regular orders,
and conditional orders were independently verified as zero after recovery.

## Timeline

Times, quantities, order identities, and account-level amounts are intentionally omitted. Relative offsets preserve the
failure ordering needed for analysis.

| Relative time | Event |
| --- | --- |
| T+00s | BTC long filled as Maker. |
| T+27s | The first ETH order reached maximum residence; cancellation started. |
| T+29s | Cancellation was verified; the executor prepared a replacement quote. |
| T+34s | ETH short replacement was accepted as `POST_ONLY`. |
| T+42s | Last normal Maker-fill wait heartbeat for the ETH order. |
| T+57s | ETH lane exited with `leg_exception:requesttimeout`. No cleanup event followed. |
| T+64s | The unmanaged ETH order filled as Maker. |
| T+72s | Closing barrier ended after a BTC position observation timeout; no close leg ran. |
| T+76s | Workflow stopped as uncertain with incomplete local turnover accounting. |

## Turnover reconciliation

Authoritative `userTrades.quoteQty` accounting after recovery:

| Leg | Maker turnover |
| --- | ---: |
| BTC open | non-zero (redacted) |
| ETH open | non-zero (redacted) |
| BTC recovery close | non-zero (redacted) |
| ETH recovery close | non-zero (redacted) |
| Total | non-zero (redacted) |

The campaign checkpoint counted only the BTC opening fill. The other three fills must be included by an independent trade
ledger/report for any campaign-level turnover total.

## Root cause

1. `execute_adaptive_maker_target()` retried order reads but directly called `position_quantity()` at the top of its loop,
   during cancellation checks, and while building its final result.
2. A single `RequestTimeout` from one of those read-only calls escaped the executor.
3. `LiveBetaVolumeService._execute_leg()` mapped every escaped exception to `submission_uncertain`; it did not retain the
   distinction between submission, order observation, position observation, and market observation.
4. A `submission_uncertain` lane is intentionally forbidden from sending another order. The close barrier therefore
   skipped ETH, but the already-known ETH order was never canceled or verified first.
5. Closing-barrier position reads had no bounded retry. One BTC read timeout prevented an otherwise determinate BTC
   position from entering Maker recovery.

## Fix

- Position and market reads now have three bounded read-only attempts with visible Chinese progress events.
- A transient position timeout cannot cause a second order submission; execution resumes against the same known order.
- Exhausted observation retries with a known active order trigger exactly one cancellation request followed by bounded
  terminal-state verification. The program never retries the order submission.
- Position-observation and market-observation failures are classified separately from submission uncertainty after cleanup.
- Opening, holding, closing-barrier, recovery, checkpoint, and final-acceptance position reads now use bounded retries.
- The terminal UI states whether it is waiting for a position, order, market, cancellation, fill, or barrier observation.
- `POST_ONLY` remains mandatory; no GTC, taker, market, or price-chasing fallback was introduced.

## Regression coverage

- A live order survives one position timeout, is observed again, and completes without resubmission.
- Persistent position timeouts cancel the known order exactly once before returning uncertain.
- Persistent unknown order state attempts cleanup and returns `cancel_not_confirmed` when terminal state cannot be proven.
- A transient position timeout at the paired closing barrier still allows both lanes to flatten.
- The full fake-client suite remains network-free.

## Live acceptance criteria

The next live validation must use a newly planned and separately confirmed campaign. It passes only if all of the following
are independently verified:

1. Every observed timeout prints the object being retried and the retry count.
2. No client-order prefix has an unexpected duplicate submission.
3. Every replacement order follows a confirmed terminal state for the prior order.
4. Every cycle ends with BTC and ETH flat and no regular or conditional orders.
5. Campaign turnover equals independent `userTrades.quoteQty` aggregation for matching order identities.
6. Maker count equals fill count; Taker and unknown-liquidity counts are zero.
7. Any unconfirmed cancellation stops the campaign and forbids further submissions on that lane.

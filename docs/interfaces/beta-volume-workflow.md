# Beta-volume application contract

This contract is the shared boundary for CLI and control-plane adapters. Presentation layers should call the application
service; they must not duplicate Beta sizing, Maker execution, fill reconciliation, or completion rules.

## Python entry points

- `BetaVolumeApplication.create_plan(BetaVolumePlanRequest, provider)`
- `BetaVolumeApplication.load_plan(plan_id)`
- `BetaVolumeApplication.execute_plan(plan, provider, event_sink=...)`
- `BetaVolumePlanStore.load_record(plan_id)` for read-only state/result inspection

The caller still owns authentication and the live mutation gates. Execution must retain dry-run planning, exact confirmation,
`--execute`, and the separate live environment opt-in.

## Plan response

The response is discriminated by:

```json
{"schema_version": 3, "kind": "beta_volume_plan", "status": "dry_run"}
```

Important fields:

- `plan`: target and per-round turnover, latched Beta allocation, reference quantities, risk bounds, recovery limits,
  expiry, leverage policy, and margin mode. Runtime quantities and automatic leverage are recalculated at each flat boundary.
- `account_readiness`: booleans and counts only; no balance amount is exposed.
- `confirm`: exact confirmation phrase derived from the stored plan.
- `safety`: explicit Maker, retry, reconciliation, and recovery properties.

## Execution response

The response is discriminated by:

```json
{"schema_version": 3, "kind": "beta_volume_execution", "mode": "live"}
```

Stable top-level fields:

| Field | Meaning |
| --- | --- |
| `status` | `executing`, `completed`, `stopped`, `uncertain`, or preflight `rejected` |
| `reason` | Machine-readable terminal/checkpoint reason |
| `executed_quote_volume` | Sum of matching authoritative `userTrades.quoteQty` |
| `target_turnover_quote` | Requested opening + closing turnover |
| `accounting` | Fill source, verification, Maker/Taker counts, fees, and realized PnL |
| `cycles` | Flat-to-flat paired-cycle summaries, timings, actual Beta ratio, and nested legs |
| `legs` | Flattened authoritative leg summaries across all cycles |
| `final_positions` | Signed BTC-long and ETH-short quantities, or null if observation failed |
| `timeline` | Sanitized lifecycle events without exchange order IDs |
| `reconciliation_required` | True when an operator must inspect exchange state |
| `retry_allowed` | Always false for the same stored plan |

`legs[*].executor_observation` is diagnostic only. Volume progress and Maker claims must come from the authoritative fields
beside it, not from executor observation counters.

## Control-plane rules

1. Advance generated-volume progress only from `accounting.verified=true` and `executed_quote_volume`.
2. Never advance progress from planned turnover alone.
3. Treat `uncertain` as sticky and require operator reconciliation; do not auto-submit another cycle.
4. Show `stopped` separately from `uncertain`: stopped is a known terminal failure, uncertain means exchange state is not
   sufficiently proven.
5. Stream or persist sanitized `timeline` events. Keep exchange order IDs and raw account payloads outside UI logs.
6. Use exactly two independent gateways. Open BTC long and ETH short concurrently, wait for the open barrier, then close
   observable exposure concurrently using actual positions.
7. Never submit again on a lane after an uncertain order outcome. A different lane with independently known state may still
   be flattened.
8. Check target completion only when both lanes are confirmed flat and have no active regular or trigger orders.
9. Use a new plan and confirmation for operator recovery; never rerun a consumed plan.
10. Before each paired opening, read available USDT and select
    `ceil(actual_opening_notional * 1.20 / available_quote)`. In auto mode the result must be at most 99x.
11. Use the coordinator client to query and configure BTC/ETH leverage serially, updating only when needed and querying again
    after each mutation. No order may be submitted until BTC isolated-long and ETH isolated-short both match the selected value;
    the two independent lane clients remain concurrent for Maker orders.

## Beta and sizing policy

- `opening_budget = round_turnover_quote / 2`
- `BTC_open_notional = opening_budget / (1 + beta)`
- `ETH_open_notional = beta * BTC_open_notional`
- The reviewed Beta is latched for the complete session. Preflight rejects stale, unavailable, invalid, non-positive, or more
  than 5% drifted snapshots.
- Confidence, confidence threshold, `status=low_confidence`, and `usable=false` remain visible metadata but are not execution
  gates.
- Quantity precision may overshoot the final target. Results expose `excess_quote`; `max_position_quote` remains a hard limit.
- The default leverage policy is `auto`; an explicit integer remains a fixed override. Balance amounts are never serialized in
  the plan, result, timeline, or terminal output.

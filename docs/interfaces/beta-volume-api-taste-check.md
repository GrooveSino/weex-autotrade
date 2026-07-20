### 1) Verdict

- Total score: 80/100
- Grade: B
- The application contract is coherent and safety-aware; the remaining weakness is the not-yet-defined HTTP error/auth adapter,
  not the trading core.

### 2) Scorecard

| Dimension | Weight | Raw (0-10) | Weighted | Why |
| --- | ---: | ---: | ---: | --- |
| Resource modeling and naming | 12 | 9 | 10.8 | `plan` and `execution` are distinct, versioned resources with one workflow vocabulary. |
| HTTP semantics correctness | 10 | 5 | 5.0 | Insufficient evidence: this change defines an application/JSON contract, not HTTP routes or status codes. |
| Contract consistency | 12 | 9 | 10.8 | Stable snake-case fields, decimal strings, millisecond timestamps, and discriminated `kind` values. |
| Error model quality | 10 | 8 | 8.0 | Terminal results have stable status/reason fields; exception-to-HTTP mapping still belongs to the web adapter. |
| API ergonomics (DX) | 8 | 8 | 6.4 | One request DTO and one application facade cover planning, loading, and execution. |
| Query/filter/sort/pagination | 8 | 8 | 6.4 | Fill reporting owns seven-day splitting, saturation handling, deduplication, and completeness. |
| Mutation safety and concurrency | 8 | 9 | 7.2 | Unique plans, atomic create, one-shot claim, exact confirmation, and no submission retry. |
| Security and auth boundaries | 8 | 8 | 6.4 | Credentials stay below the contract; the caller must retain auth and live gates. |
| Evolution and versioning | 8 | 9 | 7.2 | Discriminated schema versions allow additive evolution; auto-leverage execution uses `schema_version=3`. |
| Performance and caching | 6 | 8 | 4.8 | Reconciliation normally needs one narrow fill query per leg and bounded read-only visibility checks. |
| Observability and operability | 4 | 9 | 3.6 | Sanitized indexed timeline, source completeness, per-leg diagnostics, and reconciliation state are explicit. |
| Documentation completeness | 4 | 9 | 3.6 | Contract, defect, migration prompt, CLI examples, safety rules, and regression criteria are documented. |

### 3) Critical Defects

#### S1: The HTTP error envelope is still undefined

- Evidence: Python methods may raise `SafetyError`, `ValidationError`, Beta availability errors, or exchange exceptions; the
  current contract only standardizes returned execution records.
- Impact: a web client could receive inconsistent status codes or message-only errors and build brittle string matching.
- Fix: the control-center adapter must map domain exceptions to one versioned error envelope with `code`, `message`,
  `retryable`, `reconciliationRequired`, and an HTTP status.

#### S1: Planned turnover must not masquerade as generated turnover

- Evidence: the existing control-center progress path increments from `plan.turnover_quote`; the new contract exposes
  `accounting.verified` and `executed_quote_volume`.
- Impact: strategy progress can drift from exchange truth or advance after zero-fill/unknown-liquidity results.
- Fix: advance progress only for verified completed executions, using authoritative executed turnover.

#### S2: Caller-owned safety gates are easy to omit in a new adapter

- Evidence: `BetaVolumeApplication.execute_plan` deliberately assumes the caller already enforced auth, exact confirmation,
  and the live environment gate.
- Impact: a careless web route could invoke a financial mutation without the CLI's gate sequence.
- Fix: create one server-side command handler that validates all gates before calling the application service; do not expose
  the service directly to a browser request.

### 4) Redesign Blueprint

1. Keep `BetaVolumeApplication` as the only reusable planning/execution entry point.
2. Add a control-center command endpoint that accepts plan ID plus exact confirmation and performs server-side gates.
3. Map every thrown domain error to one versioned error envelope.
4. Persist and stream the sanitized `timeline`; never stream executor order identifiers.
5. Drive progress and completion only from authoritative accounting.
6. Add an execution-detail read endpoint backed by `BetaVolumePlanStore.load_record`, with account scoping in the web layer.

Example error envelope:

```json
{
  "schemaVersion": 1,
  "error": {
    "code": "BETA_PLAN_ALREADY_CONSUMED",
    "message": "Create and review a new plan.",
    "retryable": false,
    "reconciliationRequired": false
  }
}
```

Example progress decision:

```text
status == completed
and accounting.verified == true
and accounting.maker_only == true
=> add Decimal(executed_quote_volume)
```

### 5) Tone Block

Mode: sharp. The core contract is now disciplined; letting the web adapter invent its own errors or count planned volume would
undo the work and reintroduce the original defect at a higher layer.

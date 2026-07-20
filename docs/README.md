# Documentation

The repository documentation is grouped by ownership and purpose. Examples must use fake identifiers and public test
vectors; account-level evidence from real trading or funded wallets must be removed before committing.

## WEEX API

- [`api/README.md`](api/README.md): generated WEEX V3 documentation, endpoint index, and synchronization rules.
- [`api/UNOFFICIAL_DEMO_WEB_API.md`](api/UNOFFICIAL_DEMO_WEB_API.md): unsupported Demo Web observations and safety limits.

## Interfaces

- [`interfaces/beta-volume-workflow.md`](interfaces/beta-volume-workflow.md): stable planning and execution workflow.
- [`interfaces/beta-campaign-timeout-failure-flow.md`](interfaces/beta-campaign-timeout-failure-flow.md): timeout and failure transitions.
- [`interfaces/beta-volume-api-taste-check.md`](interfaces/beta-volume-api-taste-check.md): API contract review notes.

## Reliability

- [`reliability/live-campaign-recovery-model.md`](reliability/live-campaign-recovery-model.md): fail-closed recovery model.

## Sanitized Defects

- [`defects/2026-07-18-live-beta-volume-zero-fill-accounting.md`](defects/2026-07-18-live-beta-volume-zero-fill-accounting.md)
- [`defects/2026-07-20-live-beta-volume-read-timeout-orphan-order.md`](defects/2026-07-20-live-beta-volume-read-timeout-orphan-order.md)

Defect reports preserve failure ordering and engineering conclusions, but omit campaign IDs, order IDs, exact timestamps,
quantities, turnover, credentials, and account identifiers.

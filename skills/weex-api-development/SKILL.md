---
name: weex-api-development
description: Build, extend, debug, or review WEEX V3 USDT-contract and reverse-engineered Demo Web API integrations and CLIs, including authentication, demo trading, maker/post-only orders, active-order queries, verified cancellation, positions, leverage, margin, take-profit/stop-loss, order recovery, trade-fill and turnover reporting, CCXT mapping, and local WEEX API documentation sync. Use for work in weex-autotrade or whenever a task mentions WEEX contract endpoints, WEEX API payloads, WEEX Demo/SUSDT, trade volume, or WEEX trading safety.
---

# WEEX API Development

## Workflow

1. Locate the target repository and read its `AGENTS.md` and safety gates.
2. Search local documentation before browsing:
   - Run `rg -n "<endpoint|field|error>" docs/api skills/weex-api-development/references`.
   - Read only the matching generated page and the relevant reference below.
3. If local docs are missing or stale, run:

   ```bash
   uv run python skills/weex-api-development/scripts/sync_docs.py
   ```

4. For Demo open-order or cancellation work, read `references/demo-web-api.md`.
   Treat it as an unofficial, version-sensitive surface and confirm the current
   implementation plus tests before changing payloads.
5. Compare the official payload with the installed CCXT `weex` implementation. Do not assume CCXT sandbox support or field parity.
6. Implement through the repository gateway/service boundary. Keep exchange payload construction out of CLI presentation code.
7. Add fake-client tests. Never call WEEX from unit tests or CI.
8. Run formatting, lint, tests, and at least one no-key dry-run CLI command.

## Route By Task

- Authentication, endpoint coverage, and current fields: search `docs/api/ENDPOINTS.md`, then read the linked generated page.
- Fill lists, fees, Maker/Taker, and turnover: read `docs/api/contract/Transaction_API/GetTradeDetails.md` and `references/weex-v3-contract.md`.
- Demo/live differences and symbol rules: read `references/weex-v3-contract.md`.
- Demo regular open orders, supplemental history, and cancellation: read `references/demo-web-api.md`.
- CCXT method and raw-request mapping: read `references/ccxt-mapping.md`.
- Order mutation, retry, stop replacement, and credential rules: read `references/safety-contract.md`.
- Documentation refresh: run `scripts/sync_docs.py --help`; never hand-edit generated files under `docs/api/contract/`.

## Non-Negotiable Rules

- Treat Demo and live as separate API surfaces. Demo uses `/capi/v3/sim/*`, `SUSDT`, and symbols such as `BTCSUSDT`.
- The unofficial Demo Web API uses web-session credentials and must never fall
  back to live. Submit cancel once, then verify absence before placing another order.
- Preserve `POST_ONLY`. On rejection, report failure; never chase price or downgrade order type.
- Never retry an order submission automatically. Query by client order ID after transport uncertainty.
- Require dry-run, exact confirmation, and a separate live-environment gate for every mutation.
- Replace stops by submitting and verifying the new stop before canceling the old stop.
- Load credentials only from an explicit env file or the current project. Never scan sibling repositories.
- Never print or commit credentials, signatures, balances, account payloads, order journals, or databases.

## Documentation Authority

Generated Markdown is a development cache, not a permanent source of truth. Each page records its official URL and sync date. Re-sync before implementing behavior that may have changed, and treat a failed or partial sync as non-authoritative.

Reverse-engineered pages are not generated official documentation. Keep them
explicitly labeled unofficial, record the last repository verification date,
and update the implementation, fake-transport tests, full API note, and skill
reference together when the web client changes.

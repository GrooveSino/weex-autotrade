# WEEX Demo Web API (unofficial)

> Status: reverse-engineered from the WEEX web client and isolated behind
> `DemoWebGateway`. This is not part of the official V3 API and has no
> compatibility guarantee. Repository behavior and fixture coverage were
> reviewed on 2026-07-18; revalidate against the current web client before
> changing request fields or authentication.

## Why this surface exists

The official Demo V3 API only documents balance, positions, order placement,
and order history. The web trading client additionally exposes regular
open-order visibility and cancellation. This project uses that web surface to
close the safety gap for pure-Maker Demo execution:

| Capability | Official Demo V3 | Unofficial Demo Web |
|---|---|---|
| Balance | `GET /capi/v3/sim/balance` | Not used |
| Positions | `GET /capi/v3/sim/position/allPosition` | Not used |
| Place order | `POST /capi/v3/sim/order` | Not used |
| Order history | `GET /capi/v3/sim/order/history` | Additional history and cancel reasons |
| Regular open orders | Not documented | `POST /api/v1/private/order/getActiveOrderPage2` |
| Cancel one regular order | Not documented | `POST /api/v1/private/order/cancelOrderById` |
| Cancel all regular Demo orders | Not documented | `POST /api/v1/private/order/cancelAllOrder` |
| Trigger orders / standalone TP-SL | Not documented | Not implemented or verified |

Base URL: `https://http-gateway2.weex.com`

The Web API is not a replacement for the signed V3 API. Use V3 for Demo order
submission, positions, balance, and the primary history source. Use the Web API
only for the capabilities in the table above.

## Authentication

The Web API uses a logged-in web session, not the V3 API key signature.
Configure only the current project `.env`:

```dotenv
WEEX_WEB_CC_TOKEN=<U-TOKEN value from your own WEEX web session>
WEEX_WEB_TERMINAL_CODE=<terminalCode value from the same session>
```

Never copy these values from another account or project. Never print or commit
them. Their lifetime is controlled by WEEX and is not known or guaranteed;
authentication failure must be surfaced rather than silently falling back to a
live endpoint.

Requests are JSON `POST`s and currently reproduce these frontend headers:

| Header | Value / derivation |
|---|---|
| `U-TOKEN` | `WEEX_WEB_CC_TOKEN` |
| `terminalCode` | `WEEX_WEB_TERMINAL_CODE` |
| `terminaltype` | `1` |
| `appVersion` | `2.0.2` |
| `X-TIMESTAMP` | Unix epoch milliseconds |
| `vs` | Fresh 32-character frontend-compatible random text |
| `X-SIG` | MD5 of `weex{timestamp}{vs}1{appVersion}{terminalCode}` |
| `Origin`, `X-Origin` | `https://www.weex.com` |
| `Referer` | `https://www.weex.com/` |

MD5 is protocol-mandated here; it is not being used as a general-purpose
security primitive.

## Query regular open orders

`POST /api/v1/private/order/getActiveOrderPage2`

```json
{
  "filterCoinIdList": [64],
  "pageNo": 0,
  "pageSize": 100,
  "languageType": 1,
  "sign": "SIGN",
  "timeZone": "",
  "filterOrderStatusList": ["OPEN", "PENDING", "CANCELING"]
}
```

`64` is the observed Demo collateral coin ID. Rows are read from
`data.dataList`. Symbol filtering is deliberately performed locally from fields
such as `contractName`; the request does not send an unverified `contractId`.
Page size is bounded to 1-100.

## Query Web order history

`POST /api/v1/private/order/v2/getHistoryOrderPage`

```json
{
  "pageNo": 0,
  "pageSize": 100,
  "languageType": 1,
  "sign": "SIGN",
  "timeZone": ""
}
```

When `data.nextFlag` is true, send `data.nextKey` in the next request. Stop on
a missing/repeated key, an empty page, or the requested local limit. Deduplicate
by order ID. This surface is useful for frontend cancel reasons such as:

- `COULD_NOT_FILL`: terminal POST_ONLY rejection; do not chase price.
- `USER_CANCELED`: user/system cancellation evidence.

The official V3 history remains the primary execution record. A V3 canceled
row without a reason is not enough to infer why the order ended; the Web
history can supplement it.

## Cancel one regular order

`POST /api/v1/private/order/cancelOrderById`

```json
{
  "languageType": 1,
  "sign": "SIGN",
  "timeZone": "",
  "orderIdList": ["<exact-order-id>"]
}
```

A success response only means the cancel request was accepted. Query regular
open orders afterward and classify the result as:

- `verified_canceled`: every requested ID is absent.
- `cancel_pending`: one or more requested IDs remain active.
- `uncertain`: the mutation may have landed but verification failed.

Never submit a second cancel request merely because the first response or the
verification request timed out.

## Cancel all regular Demo orders

`POST /api/v1/private/order/cancelAllOrder`

```json
{
  "languageType": 1,
  "sign": "SIGN",
  "timeZone": "",
  "filterCoinIdList": [64],
  "filterLegacyOrderDirectionList": [
    "OPEN_LONG",
    "OPEN_SHORT",
    "CLOSE_LONG",
    "CLOSE_SHORT"
  ],
  "filterOrderStatusList": ["OPEN", "PENDING"],
  "filterContractIdList": []
}
```

Before mutation, capture the exact open-order IDs. After mutation, query open
orders and verify those IDs are absent. Symbol-scoped cancel-all is intentionally
unsupported because the `contractId` mapping has not been proven safe. To
cancel one symbol, query first and cancel exact IDs individually.

## Known incompatibilities and failure rules

- Demo placement uses symbols such as `BTCSUSDT`.
- The optional official V3 history filter has been observed to accept
  `BTCUSDT` and reject `BTCSUSDT`; keep that exception confined to history.
- Web rows may express a symbol as `BTC/SUSDT`; normalize only on the read side.
- The Web API covers regular orders only. Do not claim trigger-order or
  standalone TP/SL support.
- HTTP errors, timeouts, invalid JSON, session expiry, and schema changes fail
  closed. Never fall back to live endpoints.
- After a mutation transport error, re-query open orders/history. If the state
  cannot be proven, return `uncertain` and stop.

## CLI and implementation mapping

```bash
# Read-only regular Demo orders
./weex advanced orders open --mode demo

# Generate a cancel dry-run, then repeat with --execute and its exact phrase
./weex advanced orders cancel BTC <order-id> --mode demo

# Account-wide regular Demo cancel-all; no --symbol is accepted
./weex advanced orders cancel-all --mode demo
```

Implementation: `src/weex_cli/demo_web_gateway.py`

Behavioral tests: `tests/test_demo_web_gateway.py` and
`tests/test_demo_maker_venue.py`

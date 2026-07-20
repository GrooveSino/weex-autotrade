# Unofficial Demo Web API

Read `../../../docs/api/UNOFFICIAL_DEMO_WEB_API.md` for the complete endpoint,
header, payload, pagination, and response notes.

## Use this surface when

- The task needs regular Demo open orders.
- A Demo Maker workflow must cancel and verify an active regular order.
- V3 history reports cancellation without a useful `cancelReason`.

Continue to use official `/capi/v3/sim/*` endpoints for balance, positions,
order submission, and primary order history.

## Capability map

| Operation | Route |
|---|---|
| Regular open orders | `POST /api/v1/private/order/getActiveOrderPage2` |
| Supplemental order history | `POST /api/v1/private/order/v2/getHistoryOrderPage` |
| Cancel exact order IDs | `POST /api/v1/private/order/cancelOrderById` |
| Cancel all regular Demo orders | `POST /api/v1/private/order/cancelAllOrder` |

Base URL: `https://http-gateway2.weex.com`

Authentication uses `WEEX_WEB_CC_TOKEN` and `WEEX_WEB_TERMINAL_CODE` from the
current project only. These are web-session credentials, not V3 API keys. Never
print them, search for them outside the project, or fall back to live when they
expire.

## Required safety behavior

1. Use the official V3 API to submit the Demo order.
2. Use the Web API to observe regular active orders and supplement cancel
   reasons.
3. Send a cancel mutation at most once.
4. Re-query open orders after cancel.
5. Return `verified_canceled`, `cancel_pending`, or `uncertain`; only the first
   permits a new order.
6. Treat `COULD_NOT_FILL` as a terminal POST_ONLY rejection. Never chase.
7. Reject symbol-scoped cancel-all. Query and cancel exact IDs instead.

This surface does not establish support for Demo trigger orders, standalone
TP/SL, leverage, or margin-mode mutation.

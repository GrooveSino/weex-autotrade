# WEEX V3 Contract Notes

## Surfaces

- Base URL: `https://api-contract.weex.com`
- Live contract endpoints: `/capi/v3/*`
- Demo endpoints: `/capi/v3/sim/*`
- Authentication headers: `ACCESS-KEY`, `ACCESS-SIGN`, `ACCESS-PASSPHRASE`, `ACCESS-TIMESTAMP`
- API documentation is marked V3 Beta; refresh local snapshots before relying on mutable behavior.

## Demo

The documented Demo surface contains:

- `GET /capi/v3/sim/balance`
- `POST /capi/v3/sim/order`
- `GET /capi/v3/sim/position/allPosition`
- `GET /capi/v3/sim/order/history`

Demo uses simulated collateral `SUSDT` and symbols such as `BTCSUSDT`. The documented Demo order accepts `POST_ONLY`, attached `tpTriggerPrice`/`slTriggerPrice`, and contract/mark trigger types. It does not document Demo cancellation, leverage, margin-mode mutation, or standalone TP/SL maintenance.

As verified against the authenticated API on 2026-07-17, Demo order submission still uses `BTCSUSDT`, but `GET /capi/v3/sim/order/history` rejects that value in its optional `symbol` filter and accepts `BTCUSDT`. Keep this read-side exception isolated to Demo history queries.

## Live Orders

`POST /capi/v3/order` accepts explicit `side`, `positionSide`, `type`, quantity, price for limits, client order ID, and `timeInForce`. `POST_ONLY` is the maker-only value. Keep position side explicit and verify close-only behavior against the current official page and CCXT version.

Standalone TP/SL uses `POST /capi/v3/placeTpSlOrder`. A zero or omitted execute price means market execution; zero or omitted quantity means the full position according to the official page.

## Symbol Mapping

| Input intent | Live | Demo | CCXT swap |
|---|---|---|---|
| BTC USDT perpetual | `BTCUSDT` | `BTCSUSDT` | `BTC/USDT:USDT` |

Do not send a Demo symbol to live or silently substitute collateral.

# CCXT Mapping

The project requires CCXT `weex` V3 support. Inspect the installed version at runtime; capabilities can change independently of this skill.

| Operation | Preferred CCXT path | Raw path when needed |
|---|---|---|
| Live order | `create_order` | `POST capi/v3/order` |
| Open orders | `fetch_open_orders(..., {type: swap})` | `GET capi/v3/openOrders` |
| Trigger orders | `fetch_open_orders(..., {type: swap, trigger: true})` | `GET capi/v3/openAlgoOrders` |
| Cancel | `cancel_order` | `DELETE capi/v3/order` |
| Cancel all | `cancel_all_orders` | `DELETE capi/v3/allOpenOrders` |
| Positions | `fetch_positions` | `GET capi/v3/account/position/allPosition` |
| Margin mode | `set_margin_mode` | `POST capi/v3/account/marginType` |
| Leverage | `set_leverage` with `marginMode` | `POST capi/v3/account/leverage` |
| Close position | raw gateway method | `POST capi/v3/closePositions` |
| TP/SL | raw gateway method | `POST capi/v3/placeTpSlOrder` |
| Demo | raw gateway method | `/capi/v3/sim/*` |

CCXT reports no standard WEEX sandbox URL. Do not call `set_sandbox_mode`; use the documented Demo endpoints through the authenticated `contractPrivate` signer.

For maker orders pass `timeInForce=POST_ONLY`. Do not use OKX fields such as `tdMode`, `ordType`, or `mgnMode`.

The close endpoint accepts `symbol` and `positionId`, not `positionSide`. To close only LONG or SHORT, fetch positions, resolve exactly one active position ID for that side, then submit the documented close payload. A symbol-only close affects both sides for that symbol.

from __future__ import annotations

from decimal import Decimal
from typing import Any

from weex_cli.config import Settings
from weex_cli.demo_web_gateway import DemoWebGateway
from weex_cli.errors import UnsupportedModeError, ValidationError
from weex_cli.models import OrderIntent, decimal_text
from weex_cli.symbols import ccxt_swap_symbol, demo_symbol_id, live_symbol_id


class WeexGateway:
    """Thin WEEX/CCXT boundary with explicit demo endpoints."""

    def __init__(
        self,
        settings: Settings,
        client: Any | None = None,
        demo_web_gateway: DemoWebGateway | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._demo_web_gateway = demo_web_gateway

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = build_client(self.settings, require_private=True)
        return self._client

    def public_client(self) -> Any:
        if self._client is None:
            self._client = build_client(self.settings, require_private=False)
        return self._client

    def ticker(self, symbol: str) -> dict[str, Any]:
        return self.public_client().fetch_ticker(ccxt_swap_symbol(symbol))

    def order_book(self, symbol: str, limit: int = 10) -> dict[str, Any]:
        return self.public_client().fetch_order_book(ccxt_swap_symbol(symbol), limit)

    def balance(self, mode: str) -> Any:
        if mode == "demo":
            return self._raw("capi/v3/sim/balance", "GET")
        return self.client.fetch_balance({"type": "swap"})

    def positions(self, mode: str, symbol: str | None = None) -> list[dict[str, Any]]:
        if mode == "demo":
            rows = self._raw("capi/v3/sim/position/allPosition", "GET")
            if symbol:
                target = demo_symbol_id(symbol)
                return [row for row in rows if str(row.get("symbol", "")).upper() == target]
            return rows
        symbols = [ccxt_swap_symbol(symbol)] if symbol else None
        return self.client.fetch_positions(symbols)

    def order_history(
        self,
        mode: str,
        symbol: str | None = None,
        limit: int = 100,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if symbol and mode != "demo":
            params["symbol"] = live_symbol_id(symbol)
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        path = "capi/v3/sim/order/history" if mode == "demo" else "capi/v3/order/history"
        rows = self._raw(path, "GET", params)
        if mode == "demo" and symbol:
            if not isinstance(rows, list):
                raise ValidationError("Demo order history returned a non-list response")
            accepted = {demo_symbol_id(symbol), live_symbol_id(symbol)}
            return [row for row in rows if isinstance(row, dict) and str(row.get("symbol") or "").upper() in accepted]
        return rows

    def trade_rows(
        self,
        mode: str,
        symbol: str | None,
        *,
        start_time: int,
        end_time: int,
        limit: int,
        page: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "startTime": start_time,
            "endTime": end_time,
            "limit": limit,
        }
        if symbol and mode != "demo":
            params["symbol"] = live_symbol_id(symbol)
        if mode == "demo":
            if page is not None:
                params["page"] = page
            return self._raw("capi/v3/sim/order/history", "GET", params)
        return self._raw("capi/v3/userTrades", "GET", params)

    def open_orders(
        self,
        symbol: str | None = None,
        *,
        trigger: bool = False,
        mode: str = "live",
    ) -> list[dict[str, Any]]:
        if mode == "demo":
            if trigger:
                raise UnsupportedModeError("Demo Web trigger-order query is not implemented")
            return self.demo_web_gateway.open_orders(symbol)
        ensure_live(mode, "open orders")
        unified = ccxt_swap_symbol(symbol) if symbol else None
        return self.client.fetch_open_orders(unified, None, None, {"type": "swap", "trigger": trigger})

    def demo_web_order_history(self, symbol: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.demo_web_gateway.order_history(symbol, limit=limit)

    def place_order(self, intent: OrderIntent) -> dict[str, Any]:
        if intent.mode == "demo":
            return self._raw("capi/v3/sim/order", "POST", intent.demo_payload())
        symbol, order_type, side, quantity, price, params = intent.live_order()
        return self.client.create_order(symbol, order_type, side, quantity, price, params)

    def amount_to_precision(self, symbol: str, amount: Decimal) -> Decimal:
        value = self.public_client().amount_to_precision(ccxt_swap_symbol(symbol), str(amount))
        return Decimal(str(value))

    def price_to_precision(self, symbol: str, price: Decimal) -> Decimal:
        value = self.public_client().price_to_precision(ccxt_swap_symbol(symbol), str(price))
        return Decimal(str(value))

    def amount_step(self, symbol: str) -> Decimal:
        client = self.public_client()
        client.load_markets()
        market = client.market(ccxt_swap_symbol(symbol))
        value = (market.get("precision") or {}).get("amount")
        step = Decimal(str(value or "0"))
        if step <= 0:
            raise ValidationError("WEEX market metadata has no positive amount precision")
        return step

    def cancel_order(self, symbol: str, order_id: str, *, trigger: bool = False, mode: str = "live") -> Any:
        if mode == "demo":
            if trigger:
                raise UnsupportedModeError("Demo Web trigger-order cancellation is not implemented")
            return self.demo_web_gateway.cancel_order(order_id)
        ensure_live(mode, "cancel order")
        return self.client.cancel_order(order_id, ccxt_swap_symbol(symbol), {"type": "swap", "trigger": trigger})

    def cancel_all_orders(
        self,
        symbol: str | None = None,
        *,
        trigger: bool = False,
        mode: str = "live",
    ) -> Any:
        if mode == "demo":
            if trigger:
                raise UnsupportedModeError("Demo Web trigger-order cancellation is not implemented")
            return self.demo_web_gateway.cancel_all_orders(symbol)
        ensure_live(mode, "cancel all orders")
        unified = ccxt_swap_symbol(symbol) if symbol else None
        return self.client.cancel_all_orders(unified, {"type": "swap", "trigger": trigger})

    @property
    def demo_web_gateway(self) -> DemoWebGateway:
        if self._demo_web_gateway is None:
            self._demo_web_gateway = DemoWebGateway(self.settings)
        return self._demo_web_gateway

    def configure_position(self, symbol: str, leverage: int, margin_mode: str) -> dict[str, Any]:
        unified = ccxt_swap_symbol(symbol)
        margin_result = self.client.set_margin_mode(margin_mode, unified)
        leverage_result = self.client.set_leverage(leverage, unified, {"marginMode": margin_mode})
        return {"margin_mode": margin_result, "leverage": leverage_result}

    def close_position(self, symbol: str, position_side: str | None = None) -> Any:
        payload: dict[str, Any] = {"symbol": live_symbol_id(symbol)}
        if position_side:
            payload["positionId"] = _position_id_for_side(self.positions("live", symbol), position_side)
        return self._raw("capi/v3/closePositions", "POST", payload)

    def close_all_positions(self) -> Any:
        return self._raw("capi/v3/closePositions", "POST")

    def place_tp_sl(
        self,
        *,
        symbol: str,
        plan_type: str,
        trigger_price: str,
        position_side: str,
        client_algo_id: str,
        execute_price: str = "0",
        quantity: str = "0",
        trigger_price_type: str = "MARK_PRICE",
    ) -> Any:
        payload = {
            "symbol": live_symbol_id(symbol),
            "clientAlgoId": client_algo_id,
            "planType": plan_type,
            "triggerPrice": trigger_price,
            "executePrice": execute_price,
            "quantity": quantity,
            "positionSide": position_side.upper(),
            "triggerPriceType": trigger_price_type,
        }
        return self._raw("capi/v3/placeTpSlOrder", "POST", payload)

    def modify_tp_sl(
        self,
        *,
        order_id: str,
        trigger_price: str,
        execute_price: str = "0",
        trigger_price_type: str = "MARK_PRICE",
    ) -> Any:
        payload = {
            "orderId": order_id,
            "triggerPrice": trigger_price,
            "executePrice": execute_price,
            "triggerPriceType": trigger_price_type,
        }
        return self._raw("capi/v3/modifyTpSlOrder", "POST", payload)

    def algo_orders(self, symbol: str | None = None, *, history: bool = False) -> Any:
        params = {"symbol": live_symbol_id(symbol)} if symbol else {}
        path = "capi/v3/allAlgoOrders" if history else "capi/v3/openAlgoOrders"
        return self._raw(path, "GET", params)

    def cancel_algo_order(self, order_id: str) -> Any:
        return self._raw("capi/v3/algoOrder", "DELETE", {"orderId": order_id})

    def _raw(self, path: str, method: str, params: dict[str, Any] | None = None) -> Any:
        return self.client.request(path, "contractPrivate", method, params or {})


def build_client(settings: Settings, *, require_private: bool) -> Any:
    try:
        import ccxt
    except ModuleNotFoundError as exc:  # pragma: no cover - packaging guarantees this dependency
        raise SystemExit("Missing ccxt; run uv sync") from exc

    config: dict[str, Any] = {
        "enableRateLimit": settings.enable_rate_limit,
        "timeout": settings.timeout_ms,
        "requests_trust_env": True,
        "options": {"defaultType": "swap"},
    }
    credentials = settings.require_credentials() if require_private else settings.credentials
    if credentials.configured:
        config.update(
            {
                "apiKey": credentials.api_key,
                "secret": credentials.api_secret,
                "password": credentials.passphrase,
            }
        )
    return ccxt.weex(config)


def summarize_position_size(row: dict[str, Any]) -> str:
    raw = row.get("size", row.get("contracts", row.get("positionAmt", "0")))
    return str(decimal_text(_decimal_or_zero(raw)))


def _decimal_or_zero(value: Any):
    from decimal import Decimal, InvalidOperation

    try:
        return abs(Decimal(str(value or "0")))
    except InvalidOperation:
        return Decimal("0")


def _position_id_for_side(rows: Any, position_side: str) -> str:
    target = position_side.strip().lower()
    if target not in {"long", "short"}:
        raise ValidationError("position_side must be long or short")
    matches: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or summarize_position_size(row) == "0":
            continue
        info = row.get("info") if isinstance(row.get("info"), dict) else {}
        side = str(row.get("side") or info.get("side") or info.get("positionSide") or "").lower()
        position_id = row.get("id") or row.get("positionId") or info.get("id") or info.get("positionId")
        if side == target and position_id is not None:
            matches.append(str(position_id))
    if not matches:
        raise ValidationError(f"no active {target} position with a position ID was found")
    if len(matches) > 1:
        raise ValidationError(f"multiple active {target} positions were found; refusing ambiguous close")
    return matches[0]


def ensure_live(mode: str, operation: str) -> None:
    if mode != "live":
        raise UnsupportedModeError(f"{operation} is not exposed by the WEEX demo API")

from __future__ import annotations

from decimal import Decimal
from typing import Any

from weex_cli.core.config import Settings
from weex_cli.core.errors import UnsupportedModeError, ValidationError
from weex_cli.core.models import OrderIntent
from weex_cli.core.symbols import ccxt_swap_symbol, demo_symbol_id, live_symbol_id
from weex_cli.exchange.gateway_support import (
    _position_id_for_side,
    _weex_mutation,
    build_client,
    ensure_live,
    summarize_position_size,  # noqa: F401
)
from weex_cli.exchange.rest.demo_web import DemoWebGateway


class WeexGateway:
    """Thin WEEX/CCXT boundary with explicit demo endpoints."""

    def __init__(
        self,
        settings: Settings,
        client: Any | None = None,
        demo_web_gateway: DemoWebGateway | None = None,
        proxy_url: str | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._demo_web_gateway = demo_web_gateway
        self._proxy_url = proxy_url

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = build_client(self.settings, require_private=True, proxy_url=self._proxy_url)
        return self._client

    def public_client(self) -> Any:
        if self._client is None:
            self._client = build_client(self.settings, require_private=False, proxy_url=self._proxy_url)
        return self._client

    def account_balance_rows(self, mode: str) -> list[dict[str, Any]]:
        path = "capi/v3/sim/balance" if mode == "demo" else "capi/v3/account/balance"
        rows = self._raw(path, "GET")
        if not isinstance(rows, list):
            raise ValidationError("WEEX account balance returned a non-list response")
        return [row for row in rows if isinstance(row, dict)]

    def all_position_rows(self, mode: str) -> list[dict[str, Any]]:
        path = "capi/v3/sim/position/allPosition" if mode == "demo" else "capi/v3/account/position/allPosition"
        rows = self._raw(path, "GET")
        if not isinstance(rows, list):
            raise ValidationError("WEEX positions returned a non-list response")
        return [row for row in rows if isinstance(row, dict)]

    def close(self) -> None:
        if self._client is None:
            return
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def fork(self) -> WeexGateway:
        """Create an uninitialized gateway with the same credentials and proxy."""
        return WeexGateway(self.settings, proxy_url=self._proxy_url)

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

    def trade_rows_by_order_id(
        self,
        symbol: str,
        order_id: str,
        *,
        start_time: int,
        end_time: int,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._raw(
            "capi/v3/userTrades",
            "GET",
            {
                "symbol": live_symbol_id(symbol),
                "orderId": order_id,
                "startTime": start_time,
                "endTime": end_time,
                "limit": limit,
            },
        )

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

    def minimum_amount(self, symbol: str) -> Decimal | None:
        client = self.public_client()
        client.load_markets()
        market = client.market(ccxt_swap_symbol(symbol))
        value = ((market.get("limits") or {}).get("amount") or {}).get("min")
        if value is None:
            return None
        minimum = Decimal(str(value))
        return minimum if minimum.is_finite() and minimum > 0 else None

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
        margin_result = self.configure_margin_mode(symbol, margin_mode)
        leverage_result = self.configure_leverage(symbol, leverage, margin_mode)
        return {"margin_mode": margin_result, "leverage": leverage_result}

    def configure_margin_mode(self, symbol: str, margin_mode: str) -> Any:
        unified = ccxt_swap_symbol(symbol)
        return _weex_mutation(lambda: self.client.set_margin_mode(margin_mode, unified))

    def configure_leverage(self, symbol: str, leverage: int, margin_mode: str) -> Any:
        unified = ccxt_swap_symbol(symbol)
        return _weex_mutation(lambda: self.client.set_leverage(leverage, unified, {"marginMode": margin_mode}))

    def leverage(self, symbol: str) -> dict[str, Any]:
        row = self.client.fetch_leverage(ccxt_swap_symbol(symbol))
        if not isinstance(row, dict):
            raise ValidationError("WEEX leverage configuration returned a non-object response")
        return row

    def close_position(self, symbol: str, position_side: str | None = None) -> Any:
        payload: dict[str, Any] = {"symbol": live_symbol_id(symbol)}
        if position_side:
            payload["positionId"] = _position_id_for_side(self.positions("live", symbol), position_side)
        return self._raw("capi/v3/closePositions", "POST", payload)

    def close_position_id(self, symbol: str, position_id: str) -> Any:
        normalized = str(position_id).strip()
        if not normalized.isdigit() or len(normalized) > 20:
            raise ValidationError("WEEX position ID is invalid")
        return self._raw(
            "capi/v3/closePositions",
            "POST",
            {"symbol": live_symbol_id(symbol), "positionId": int(normalized)},
        )

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

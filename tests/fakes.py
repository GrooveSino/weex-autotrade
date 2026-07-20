from __future__ import annotations

from decimal import Decimal
from typing import Any


class FakeExchange:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.responses: dict[tuple[str, str], Any] = {}
        self.position_rows: list[dict[str, Any]] = []

    def request(self, path: str, api: str, method: str, params: dict[str, Any]):
        self.calls.append(("request", path, api, method, params))
        return self.responses.get((method, path), {"path": path, "params": params})

    def fetch_ticker(self, symbol: str):
        self.calls.append(("fetch_ticker", symbol))
        return {"symbol": symbol, "last": 100.0}

    def fetch_order_book(self, symbol: str, limit: int):
        self.calls.append(("fetch_order_book", symbol, limit))
        return {"symbol": symbol, "bids": [[99, 1]], "asks": [[101, 1]]}

    def amount_to_precision(self, symbol: str, amount: str):
        self.calls.append(("amount_to_precision", symbol, amount))
        return str(Decimal(amount).quantize(Decimal("0.0001")))

    def fetch_balance(self, params: dict[str, Any]):
        self.calls.append(("fetch_balance", params))
        return {"USDT": {"free": 10}}

    def fetch_positions(self, symbols=None):
        self.calls.append(("fetch_positions", symbols))
        return self.position_rows

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        self.calls.append(("fetch_open_orders", symbol, since, limit, params))
        return []

    def create_order(self, symbol, order_type, side, quantity, price, params):
        self.calls.append(("create_order", symbol, order_type, side, quantity, price, params))
        return {"id": "live-order", "clientOrderId": params["clientOrderId"]}

    def cancel_order(self, order_id, symbol, params):
        self.calls.append(("cancel_order", order_id, symbol, params))
        return {"id": order_id, "status": "canceled"}

    def cancel_all_orders(self, symbol, params):
        self.calls.append(("cancel_all_orders", symbol, params))
        return []

    def set_margin_mode(self, mode, symbol):
        self.calls.append(("set_margin_mode", mode, symbol))
        return {"mode": mode}

    def set_leverage(self, leverage, symbol, params):
        self.calls.append(("set_leverage", leverage, symbol, params))
        return {"leverage": leverage}

    def fetch_leverage(self, symbol):
        self.calls.append(("fetch_leverage", symbol))
        return {"marginMode": "isolated", "longLeverage": 10, "shortLeverage": 10}

    def close_position(self, symbol, side, params):
        self.calls.append(("close_position", symbol, side, params))
        return {"symbol": symbol, "closed": True}

    def close_all_positions(self, params):
        self.calls.append(("close_all_positions", params))
        return []

"""Bounded parallel reader for the launch-time BTC/ETH account boundary."""

from __future__ import annotations

import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, wait
from decimal import Decimal
from typing import Any

from weex_cli.control_api.exchange import ValidationError, WeexGateway, summarize_position_size

from fleet_api.execution.runtime.execution_io import NORMAL_IO_PRIORITY, BoundedGateway, ExecutionIoBudget


class AccountBoundaryReader:
    """Read independent balance/BTC/ETH lanes without blocking the actor loop."""

    def __init__(self, budget: ExecutionIoBudget, *, max_workers: int) -> None:
        self._budget = budget
        self._pool = ThreadPoolExecutor(
            max_workers=max(3, max_workers),
            thread_name_prefix="fleet-boundary",
        )

    def read(self, gateway: WeexGateway) -> dict[str, object]:
        root = BoundedGateway(gateway, self._budget, NORMAL_IO_PRIORITY)
        lanes = {symbol: root.fork() for symbol in ("BTC", "ETH")}
        try:
            available_future = self._pool.submit(_available_quote, root)
            symbol_futures = {
                symbol: self._pool.submit(_symbol_boundary, lane, symbol) for symbol, lane in lanes.items()
            }
            wait((available_future, *symbol_futures.values()))
            available = available_future.result()
            symbols = [symbol_futures[symbol].result() for symbol in ("BTC", "ETH")]
        finally:
            for lane in lanes.values():
                lane.close()

        positions = [position for result in symbols for position in result["positions"]]
        position_count = sum(int(result["position_count"]) for result in symbols)
        regular_count = sum(int(result["regular_order_count"]) for result in symbols)
        trigger_count = sum(int(result["trigger_order_count"]) for result in symbols)
        return {
            "flat": position_count == 0 and regular_count == 0 and trigger_count == 0,
            "position_count": position_count,
            "regular_order_count": regular_count,
            "trigger_order_count": trigger_count,
            "available_quote": _decimal_text(available),
            "blocking_positions": positions,
            "checked_at_ms": time.time_ns() // 1_000_000,
        }

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)


def _available_quote(gateway: BoundedGateway) -> Decimal:
    rows = gateway.account_balance_rows("live")
    usdt = next((row for row in rows if str(row.get("asset") or "").upper() == "USDT"), None)
    if usdt is None:
        raise ValidationError("WEEX balance response has no USDT row")
    return _decimal(usdt.get("availableBalance"), "available USDT")


def _symbol_boundary(gateway: BoundedGateway, symbol: str) -> dict[str, object]:
    rows = gateway.positions("live", symbol)
    positions: list[dict[str, str]] = []
    for row in rows:
        quantity = abs(_decimal(summarize_position_size(row), f"{symbol} position size"))
        if quantity <= 0:
            continue
        info = row.get("info") if isinstance(row.get("info"), Mapping) else {}
        side = str(row.get("side") or info.get("positionSide") or info.get("side") or "unknown").lower()
        positions.append(
            {
                "symbol": symbol,
                "side": side if side in {"long", "short"} else "unknown",
                "quantity": _decimal_text(quantity),
                "approximate_quote": _decimal_text(_position_notional(row, info, quantity)),
            }
        )
    return {
        "positions": positions,
        "position_count": len(positions),
        "regular_order_count": len(gateway.open_orders(symbol, mode="live")),
        "trigger_order_count": _row_count(gateway.algo_orders(symbol)),
    }


def _position_notional(row: Mapping[str, Any], info: Mapping[str, Any], quantity: Decimal) -> Decimal:
    for key in ("notional", "openValue", "positionValue"):
        value = _positive_decimal(row.get(key) if row.get(key) is not None else info.get(key))
        if value is not None:
            return value
    for key in ("markPrice", "entryPrice"):
        price = _positive_decimal(row.get(key) if row.get(key) is not None else info.get(key))
        if price is not None:
            return quantity * price
    return Decimal(0)


def _positive_decimal(value: object) -> Decimal | None:
    try:
        parsed = abs(Decimal(str(value)))
    except Exception:  # noqa: BLE001 - incomplete public position metadata is tolerated
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _decimal(value: object, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - normalize exchange payload validation
        raise ValidationError(f"WEEX {name} is not numeric") from exc
    if not parsed.is_finite():
        raise ValidationError(f"WEEX {name} is not finite")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _row_count(value: object) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, Mapping):
        rows = value.get("rows") or value.get("data") or value.get("list") or []
        return len(rows) if isinstance(rows, list) else 0
    return 0

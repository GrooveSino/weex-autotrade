from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from weex_cli.config import Mode, normalize_mode
from weex_cli.errors import ValidationError
from weex_cli.gateway import WeexGateway
from weex_cli.models import decimal_text
from weex_cli.symbols import demo_symbol_id, live_symbol_id

DAY_MS = 24 * 60 * 60 * 1000
LIVE_WINDOW_MS = 7 * DAY_MS
DEMO_WINDOW_MS = 90 * DAY_MS
LIVE_LIMIT = 100
DEMO_LIMIT = 1000
MAX_REQUESTS = 500


class TradeReportService:
    def __init__(self, gateway: WeexGateway) -> None:
        self.gateway = gateway

    def report(
        self,
        *,
        mode: str,
        symbol: str | None,
        start_time: int,
        end_time: int,
    ) -> dict[str, Any]:
        selected = normalize_mode(mode)
        _validate_range(selected, start_time, end_time)
        if selected == "demo":
            raw_rows, complete, warnings = self._demo_rows(symbol, start_time, end_time)
            source = "demo_order_history"
            granularity = "order"
            warnings.insert(
                0,
                "WEEX exposes no Demo fill endpoint; Demo volume is aggregated from executed order history.",
            )
        else:
            raw_rows, complete, warnings = self._live_rows(symbol, start_time, end_time)
            source = "user_trades"
            granularity = "fill"

        trades = _normalize_rows(raw_rows, selected, start_time, end_time)
        if selected == "demo" and symbol:
            accepted_symbols = {demo_symbol_id(symbol), live_symbol_id(symbol)}
            trades = [trade for trade in trades if trade["symbol"] in accepted_symbols]
        return {
            "mode": selected,
            "symbol": symbol.upper() if symbol else None,
            "source": source,
            "granularity": granularity,
            "start_time": start_time,
            "start_datetime": _datetime_text(start_time),
            "end_time": end_time,
            "end_datetime": _datetime_text(end_time),
            "complete": complete,
            "warnings": warnings,
            "summary": _summary(trades, selected),
            "trades": trades,
        }

    def _demo_rows(
        self, symbol: str | None, start_time: int, end_time: int
    ) -> tuple[list[dict[str, Any]], bool, list[str]]:
        collected: list[dict[str, Any]] = []
        requests = 0
        for window_start, window_end in _windows(start_time, end_time, DEMO_WINDOW_MS):
            page = 0
            while requests < MAX_REQUESTS:
                batch = self.gateway.trade_rows(
                    "demo",
                    symbol,
                    start_time=window_start,
                    end_time=window_end,
                    limit=DEMO_LIMIT,
                    page=page,
                )
                requests += 1
                collected.extend(row for row in batch if isinstance(row, dict))
                if len(batch) < DEMO_LIMIT:
                    break
                page += 1
            else:
                return collected, False, [f"Stopped after {MAX_REQUESTS} API requests; totals are incomplete."]
        return collected, True, []

    def _live_rows(
        self, symbol: str | None, start_time: int, end_time: int
    ) -> tuple[list[dict[str, Any]], bool, list[str]]:
        pending = list(_windows(start_time, end_time, LIVE_WINDOW_MS))
        collected: list[dict[str, Any]] = []
        warnings: list[str] = []
        requests = 0
        complete = True
        while pending:
            if requests >= MAX_REQUESTS:
                complete = False
                warnings.append(f"Stopped after {MAX_REQUESTS} API requests; totals are incomplete.")
                break
            window_start, window_end = pending.pop()
            batch = self.gateway.trade_rows(
                "live",
                symbol,
                start_time=window_start,
                end_time=window_end,
                limit=LIVE_LIMIT,
            )
            requests += 1
            if len(batch) >= LIVE_LIMIT and window_start < window_end:
                midpoint = (window_start + window_end) // 2
                pending.append((midpoint + 1, window_end))
                pending.append((window_start, midpoint))
                continue
            collected.extend(row for row in batch if isinstance(row, dict))
            if len(batch) >= LIVE_LIMIT:
                complete = False
                warnings.append(
                    f"At least {LIVE_LIMIT} fills share millisecond {window_start}; that millisecond may be truncated."
                )
        return collected, complete, warnings


def parse_timestamp(value: str, *, name: str) -> int:
    text = str(value).strip()
    if re.fullmatch(r"(?:\d{10}|\d{13})", text):
        timestamp = int(text)
        if len(text) == 10:
            timestamp *= 1000
        return timestamp
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{name} must be Unix seconds/milliseconds or ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{name} ISO-8601 value must include a timezone offset")
    return int(parsed.timestamp() * 1000)


def current_timestamp_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _validate_range(mode: Mode, start_time: int, end_time: int) -> None:
    if start_time < 0 or end_time < 0:
        raise ValidationError("timestamps must be nonnegative")
    if end_time < start_time:
        raise ValidationError("end_time must be greater than or equal to start_time")
    if mode == "live" and start_time < current_timestamp_ms() - 365 * DAY_MS:
        raise ValidationError("WEEX live trade history is limited to the most recent 365 days")


def _windows(start_time: int, end_time: int, size: int):
    cursor = start_time
    while cursor <= end_time:
        window_end = min(end_time, cursor + size - 1)
        yield cursor, window_end
        if window_end == end_time:
            break
        cursor = window_end + 1


def _normalize_rows(rows: list[dict[str, Any]], mode: Mode, start_time: int, end_time: int) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for row in rows:
        trade = _normalize_row(row, mode)
        if trade is None:
            continue
        timestamp = int(trade["timestamp"])
        if not start_time <= timestamp <= end_time:
            continue
        identity = (str(trade["trade_id"]), str(trade["order_id"]), timestamp)
        if identity in seen:
            continue
        seen.add(identity)
        normalized.append(trade)
    return sorted(normalized, key=lambda item: (int(item["timestamp"]), str(item["trade_id"])))


def _normalize_row(row: dict[str, Any], mode: Mode) -> dict[str, Any] | None:
    demo = mode == "demo"
    quantity = _decimal(row.get("executedQty") if demo else row.get("qty"))
    if quantity <= 0:
        return None
    price = _decimal(row.get("avgPrice") if demo else row.get("price"))
    if price <= 0:
        price = _decimal(row.get("price"))
    quote_quantity = _decimal(row.get("cumQuote") if demo else row.get("quoteQty"))
    if quote_quantity <= 0:
        quote_quantity = price * quantity
    timestamp = _integer(row.get("updateTime") if demo else row.get("time"))
    if timestamp is None:
        timestamp = _integer(row.get("time"))
    if timestamp is None:
        return None

    side = str(row.get("side") or "").upper()
    position_side = str(row.get("positionSide") or "").upper()
    maker = _optional_bool(row.get("maker"))
    if demo and maker is None and str(row.get("timeInForce") or "").upper() == "POST_ONLY":
        maker = True
    order_id = row.get("orderId") or row.get("id") or ""
    trade_id = row.get("id") if not demo else order_id
    return {
        "trade_id": str(trade_id or ""),
        "order_id": str(order_id),
        "symbol": str(row.get("symbol") or "").upper(),
        "side": side,
        "position_side": position_side,
        "position_action": _position_action(side, position_side),
        "price": decimal_text(price),
        "quantity": decimal_text(quantity),
        "quote_quantity": decimal_text(quote_quantity),
        "maker": maker,
        "commission": decimal_text(_decimal(row.get("commission"))),
        "commission_asset": row.get("commissionAsset"),
        "realized_pnl": decimal_text(_decimal(row.get("realizedPnl"))),
        "status": row.get("status"),
        "timestamp": timestamp,
        "datetime": _datetime_text(timestamp),
    }


def _summary(trades: list[dict[str, Any]], mode: Mode) -> dict[str, Any]:
    totals = defaultdict(Decimal)
    base_by_symbol = defaultdict(Decimal)
    commission_by_asset = defaultdict(Decimal)
    order_ids: set[str] = set()
    for trade in trades:
        quote = _decimal(trade["quote_quantity"])
        quantity = _decimal(trade["quantity"])
        totals["quote_volume"] += quote
        totals[f"{str(trade['side']).lower()}_quote_volume"] += quote
        totals[f"action_{trade['position_action']}_quote_volume"] += quote
        liquidity = "maker" if trade["maker"] is True else "taker" if trade["maker"] is False else "unknown"
        totals[f"liquidity_{liquidity}_quote_volume"] += quote
        totals[f"liquidity_{liquidity}_count"] += 1
        totals["realized_pnl"] += _decimal(trade["realized_pnl"])
        base_by_symbol[str(trade["symbol"])] += quantity
        if trade["order_id"]:
            order_ids.add(str(trade["order_id"]))
        asset = trade.get("commission_asset")
        if asset:
            commission_by_asset[str(asset)] += _decimal(trade["commission"])
    return {
        "trade_count": len(trades),
        "order_count": len(order_ids),
        "quote_asset": "SUSDT" if mode == "demo" else "USDT",
        "total_quote_volume": decimal_text(totals["quote_volume"]),
        "opening_quote_volume": decimal_text(totals["action_open_quote_volume"]),
        "closing_quote_volume": decimal_text(totals["action_close_quote_volume"]),
        "unknown_action_quote_volume": decimal_text(totals["action_unknown_quote_volume"]),
        "buy_quote_volume": decimal_text(totals["buy_quote_volume"]),
        "sell_quote_volume": decimal_text(totals["sell_quote_volume"]),
        "maker_quote_volume": decimal_text(totals["liquidity_maker_quote_volume"]),
        "taker_quote_volume": decimal_text(totals["liquidity_taker_quote_volume"]),
        "unknown_liquidity_quote_volume": decimal_text(totals["liquidity_unknown_quote_volume"]),
        "maker_count": int(totals["liquidity_maker_count"]),
        "taker_count": int(totals["liquidity_taker_count"]),
        "unknown_liquidity_count": int(totals["liquidity_unknown_count"]),
        "base_quantity_by_symbol": {symbol: decimal_text(value) for symbol, value in sorted(base_by_symbol.items())},
        "commission_by_asset": {asset: decimal_text(value) for asset, value in sorted(commission_by_asset.items())},
        "realized_pnl": decimal_text(totals["realized_pnl"]),
    }


def _position_action(side: str, position_side: str) -> str:
    if (position_side, side) in {("LONG", "BUY"), ("SHORT", "SELL")}:
        return "open"
    if (position_side, side) in {("LONG", "SELL"), ("SHORT", "BUY")}:
        return "close"
    return "unknown"


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return result if result.is_finite() else Decimal("0")


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if str(value).strip().lower() in {"true", "1"}:
        return True
    if str(value).strip().lower() in {"false", "0"}:
        return False
    return None


def _datetime_text(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp / 1000, tz=UTC).isoformat(timespec="milliseconds")

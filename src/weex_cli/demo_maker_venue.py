from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from weex_cli.adaptive_executor import VenueOrder
from weex_cli.adaptive_maker import MarketSnapshot, Side
from weex_cli.errors import SubmissionUncertainError, ValidationError
from weex_cli.gateway import WeexGateway, summarize_position_size
from weex_cli.models import OrderIntent
from weex_cli.service import TradingService

MIN_SUBMIT_INTERVAL_SECONDS = 10.1
OPEN_ORDER_QUERY_INTERVAL_SECONDS = 3.0
WEB_ERROR_COOLDOWN_SECONDS = 15.0


class DemoAdaptiveMakerVenue:
    """Real WEEX Demo venue using V3 execution and the authenticated Web order surface."""

    def __init__(
        self,
        gateway: WeexGateway,
        symbol: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.gateway = gateway
        self.symbol = symbol
        self.trading = TradingService(gateway)
        self.clock = clock
        self.sleep = sleep
        self._last_mid: float | None = None
        self._last_submit_at: float | None = None
        self._last_open_query_at: float | None = None
        self._web_unavailable_until = 0.0
        self._known_orders: dict[str, VenueOrder] = {}

    @property
    def now_ms(self) -> int:
        return round(self.clock() * 1000)

    def snapshot(self) -> MarketSnapshot:
        book = self.gateway.order_book(self.symbol, 5)
        bids = _levels(book.get("bids"))
        asks = _levels(book.get("asks"))
        if not bids or not asks:
            raise ValidationError("WEEX order book is missing bids or asks")
        bid, bid_size = bids[0]
        ask, ask_size = asks[0]
        tick_size = _tick_size(bids, asks, bid, ask)
        mid = (bid + ask) / 2
        volatility_ticks = 1.0
        if self._last_mid is not None:
            volatility_ticks = max(1.0, abs(mid - self._last_mid) / tick_size)
        self._last_mid = mid
        return MarketSnapshot(
            timestamp_ms=self.now_ms,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            buy_flow_per_sec=max(0.001, ask_size * 0.5),
            sell_flow_per_sec=max(0.001, bid_size * 0.5),
            volatility_ticks=volatility_ticks,
            tick_size=tick_size,
        )

    def position_quantity(self) -> float:
        rows = self.gateway.positions("demo", self.symbol)
        active: list[tuple[str, Decimal]] = []
        for row in rows:
            quantity = Decimal(summarize_position_size(row))
            if quantity <= 0:
                continue
            active.append((str(row.get("side") or "").upper(), quantity))
        if any(side != "LONG" for side, _ in active):
            raise ValidationError("adaptive Demo Maker supports only a single LONG position")
        if len(active) > 1:
            raise ValidationError("multiple active Demo positions found")
        return float(active[0][1]) if active else 0.0

    def wait_for_submission_slot(self) -> None:
        self._throttle_submission()

    def submit_post_only(self, side: Side, quantity: float, price: float, client_order_id: str) -> VenueOrder:
        self._throttle_submission()
        precise_quantity = self.gateway.amount_to_precision(self.symbol, Decimal(str(quantity)))
        precise_price = self.gateway.price_to_precision(self.symbol, Decimal(str(price)))
        if precise_quantity <= 0:
            raise ValidationError("adaptive order quantity is below WEEX precision")
        if precise_price <= 0:
            raise ValidationError("adaptive order price is below WEEX precision")
        try:
            latest = self.snapshot()
        except Exception as exc:  # noqa: BLE001 - no order was submitted; executor applies bounded read-only backoff
            return VenueOrder(
                order_id="",
                client_order_id=client_order_id,
                side=side,
                price=float(precise_price),
                quantity=float(precise_quantity),
                filled_quantity=0.0,
                cumulative_quote=0.0,
                status="not_submitted",
                post_only=True,
                maker=None,
                cancellation_reason=f"LOCAL_BOOK_UNAVAILABLE:{type(exc).__name__}",
            )
        if side == "buy":
            remains_post_only = precise_price < Decimal(str(latest.ask))
        else:
            remains_post_only = precise_price > Decimal(str(latest.bid))
        if not remains_post_only:
            return VenueOrder(
                order_id="",
                client_order_id=client_order_id,
                side=side,
                price=float(precise_price),
                quantity=float(precise_quantity),
                filled_quantity=0.0,
                cumulative_quote=0.0,
                status="not_submitted",
                post_only=True,
                maker=None,
                cancellation_reason="LOCAL_PRICE_WOULD_TAKE",
            )
        intent = OrderIntent.create(
            mode="demo",
            symbol=self.symbol,
            side=side,
            position_side="long",
            order_type="limit",
            quantity=precise_quantity,
            price=precise_price,
            time_in_force="POST_ONLY",
            client_order_id=client_order_id,
            reduce_only=side == "sell",
        )
        self._last_submit_at = self.clock()
        submission = self.trading.submit_order(intent, allow_existing=True)
        raw = submission.get("result") or submission.get("order") or {}
        if not isinstance(raw, Mapping):
            raise ValidationError("WEEX Demo submission returned no order object")
        if raw.get("success") is False:
            return VenueOrder(
                order_id=str(raw.get("orderId") or ""),
                client_order_id=client_order_id,
                side=side,
                price=float(precise_price),
                quantity=float(precise_quantity),
                filled_quantity=0.0,
                cumulative_quote=0.0,
                status="rejected",
                post_only=True,
                maker=None,
            )
        order_id = str(raw.get("orderId") or raw.get("id") or "")
        if not order_id:
            raise ValidationError("WEEX Demo submission returned no order ID")
        order = VenueOrder(
            order_id=order_id,
            client_order_id=client_order_id,
            side=side,
            price=float(precise_price),
            quantity=float(precise_quantity),
            filled_quantity=0.0,
            cumulative_quote=0.0,
            status="new",
            post_only=True,
            maker=None,
        )
        self._last_open_query_at = None
        return self._remember(order)

    def fetch_order(self, order_id: str, client_order_id: str) -> VenueOrder:
        cached = self._known_order(order_id, client_order_id)
        rows = self.gateway.order_history("demo", None, limit=100)
        v3_canceled: VenueOrder | None = None
        for row in rows:
            if not _same_order(row, order_id, client_order_id):
                continue
            order = _venue_order(row)
            if order.status != "canceled" or order.cancellation_reason:
                return self._remember(order)
            v3_canceled = order

        if v3_canceled is not None:
            if self.clock() >= self._web_unavailable_until:
                try:
                    for row in self.gateway.demo_web_order_history(None, limit=100):
                        if _same_order(row, order_id, client_order_id):
                            return self._remember(_history_order(row))
                except Exception:  # noqa: BLE001 - preserve V3 terminal evidence during Web cooldown
                    self._start_web_cooldown()
            return self._remember(_unknown_order(v3_canceled, cancellation_reason="V3_CANCELED_REASON_UNKNOWN"))

        now = self.clock()
        if now < self._web_unavailable_until:
            if cached is not None:
                return _unknown_order(cached, cancellation_reason="WEB_VISIBILITY_COOLDOWN")
            raise SubmissionUncertainError("WEEX Demo Web order visibility is cooling down after an error")
        if (
            cached is not None
            and self._last_open_query_at is not None
            and now - self._last_open_query_at < OPEN_ORDER_QUERY_INTERVAL_SECONDS
        ):
            return cached

        try:
            for row in self.gateway.open_orders(None, mode="demo"):
                if _same_order(row, order_id, client_order_id):
                    self._last_open_query_at = self.clock()
                    return self._remember(_venue_order(row))
        except Exception as exc:  # noqa: BLE001 - terminal histories remain safe read-only fallbacks
            self._start_web_cooldown()
            if cached is not None:
                return _unknown_order(cached, cancellation_reason="WEB_VISIBILITY_COOLDOWN")
            raise SubmissionUncertainError(
                f"WEEX Demo open-order visibility is unavailable: {type(exc).__name__}"
            ) from exc
        self._last_open_query_at = self.clock()

        try:
            for row in self.gateway.demo_web_order_history(None, limit=100):
                if _same_order(row, order_id, client_order_id):
                    return self._remember(_history_order(row))
        except Exception:  # noqa: BLE001 - active-order absence remains useful evidence
            self._start_web_cooldown()
        absent = cached or VenueOrder(order_id, client_order_id, "buy", 0, 0, 0, 0, "unknown", True, None)
        return self._remember(_unknown_order(absent, cancellation_reason="OPEN_ORDER_ABSENT"))

    def cancel_order(self, order_id: str, client_order_id: str) -> VenueOrder:
        cached = self._known_order(order_id, client_order_id) or VenueOrder(
            order_id, client_order_id, "buy", 0, 0, 0, 0, "unknown", True, None
        )
        try:
            result = self.gateway.cancel_order(self.symbol, order_id, mode="demo")
        except SubmissionUncertainError:
            self._start_web_cooldown()
            raise
        if isinstance(result, Mapping) and result.get("status") == "verified_canceled":
            self._last_open_query_at = self.clock()
            return self._remember(_unknown_order(cached, cancellation_reason="OPEN_ORDER_ABSENT"))
        return self._remember(_unknown_order(cached, cancellation_reason="CANCEL_UNCONFIRMED"))

    def advance(self, milliseconds: int) -> None:
        self.sleep(milliseconds / 1000)

    def _throttle_submission(self) -> None:
        if self._last_submit_at is None:
            return
        remaining = MIN_SUBMIT_INTERVAL_SECONDS - (self.clock() - self._last_submit_at)
        if remaining > 0:
            self.sleep(remaining)

    def _known_order(self, order_id: str, client_order_id: str) -> VenueOrder | None:
        return self._known_orders.get(order_id) or self._known_orders.get(client_order_id)

    def _remember(self, order: VenueOrder) -> VenueOrder:
        if order.order_id:
            self._known_orders[order.order_id] = order
        if order.client_order_id:
            self._known_orders[order.client_order_id] = order
        return order

    def _start_web_cooldown(self) -> None:
        self._web_unavailable_until = max(self._web_unavailable_until, self.clock() + WEB_ERROR_COOLDOWN_SECONDS)


def _levels(value: Any) -> list[tuple[float, float]]:
    rows: list[tuple[float, float]] = []
    for row in value if isinstance(value, list) else []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price, quantity = float(row[0]), float(row[1])
        if math.isfinite(price) and math.isfinite(quantity) and price > 0 and quantity >= 0:
            rows.append((price, quantity))
    return rows


def _tick_size(bids: list[tuple[float, float]], asks: list[tuple[float, float]], bid: float, ask: float) -> float:
    prices = sorted({price for price, _ in [*bids, *asks]})
    differences = [right - left for left, right in zip(prices, prices[1:], strict=False) if right > left]
    return min(differences) if differences else ask - bid


def _same_order(row: Mapping[str, Any], order_id: str, client_order_id: str) -> bool:
    return (
        str(row.get("id") or row.get("orderId") or "") == order_id
        or str(row.get("clientOrderId") or "") == client_order_id
    )


def _venue_order(row: Mapping[str, Any]) -> VenueOrder:
    status_text = str(row.get("status") or "").upper()
    status = {
        "OPEN": "new",
        "PENDING": "new",
        "CANCELING": "new",
        "PARTIALLY_FILLED": "partially_filled",
        "FILLED": "filled",
        "CANCELED": "canceled",
        "CANCELLED": "canceled",
        "REJECTED": "rejected",
        "EXPIRED": "rejected",
    }.get(status_text, "unknown")
    side = str(row.get("orderSide") or row.get("side") or "").lower()
    normalized_side: Side = "sell" if side == "sell" else "buy"
    price = float(row.get("price") or row.get("avgPrice") or 0)
    quantity = float(row.get("size") or row.get("origQty") or 0)
    filled = float(row.get("cumFillSize") or row.get("executedQty") or 0)
    quote = float(row.get("cumFillValue") or row.get("cumQuote") or 0)
    post_only = str(row.get("timeInForce") or "").upper() == "POST_ONLY"
    maker = True if filled > 0 and post_only else None
    return VenueOrder(
        order_id=str(row.get("id") or row.get("orderId") or ""),
        client_order_id=str(row.get("clientOrderId") or ""),
        side=normalized_side,
        price=price,
        quantity=quantity,
        filled_quantity=filled,
        cumulative_quote=quote,
        status=status,  # type: ignore[arg-type]
        post_only=post_only,
        maker=maker,
        cancellation_reason=str(row.get("cancelReason") or "").upper() or None,
    )


def _history_order(row: Mapping[str, Any]) -> VenueOrder:
    order = _venue_order(row)
    if order.status == "canceled" and not order.cancellation_reason:
        return _unknown_order(order, cancellation_reason="CANCELED_REASON_UNKNOWN")
    return order


def _unknown_order(order: VenueOrder, *, cancellation_reason: str | None = None) -> VenueOrder:
    return VenueOrder(
        order.order_id,
        order.client_order_id,
        order.side,
        order.price,
        order.quantity,
        order.filled_quantity,
        order.cumulative_quote,
        "unknown",
        order.post_only,
        order.maker,
        order.queue_ahead,
        cancellation_reason or order.cancellation_reason,
    )

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from decimal import Decimal

from weex_cli.core.errors import ValidationError
from weex_cli.core.models import OrderIntent
from weex_cli.exchange.rest.gateway import WeexGateway, summarize_position_size
from weex_cli.execution.adaptive import VenueOrder
from weex_cli.execution.adaptive_maker import MarketSnapshot, Side
from weex_cli.execution.service import TradingService

from .demo_orders import DemoOrderReconciliationMixin
from .demo_support import levels, not_submitted
from .demo_support import tick_size as calculate_tick_size

MIN_SUBMIT_INTERVAL_SECONDS = 10.1
WEB_ERROR_COOLDOWN_SECONDS = 15.0


class DemoAdaptiveMakerVenue(DemoOrderReconciliationMixin):
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
        bids = levels(book.get("bids"))
        asks = levels(book.get("asks"))
        if not bids or not asks:
            raise ValidationError("WEEX order book is missing bids or asks")
        bid, bid_size = bids[0]
        ask, ask_size = asks[0]
        tick_size = calculate_tick_size(bids, asks, bid, ask)
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
            return not_submitted(
                side,
                client_order_id,
                float(precise_quantity),
                float(precise_price),
                f"LOCAL_BOOK_UNAVAILABLE:{type(exc).__name__}",
            )
        if side == "buy":
            remains_post_only = precise_price < Decimal(str(latest.ask))
        else:
            remains_post_only = precise_price > Decimal(str(latest.bid))
        if not remains_post_only:
            return not_submitted(
                side,
                client_order_id,
                float(precise_quantity),
                float(precise_price),
                "LOCAL_PRICE_WOULD_TAKE",
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

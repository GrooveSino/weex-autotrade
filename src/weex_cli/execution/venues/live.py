from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from weex_cli.core.errors import SubmissionUncertainError, ValidationError
from weex_cli.core.models import OrderIntent
from weex_cli.core.reliability import FAST_READ_RETRY_POLICY, retry_read
from weex_cli.exchange.rest.gateway import WeexGateway, summarize_position_size
from weex_cli.execution.adaptive import ProgressSink, VenueOrder
from weex_cli.execution.adaptive_maker import MarketSnapshot, Side
from weex_cli.execution.service import TradingService
from weex_cli.live_websocket import MarketStreamUnavailable

from .live_orders import LiveOrderReconciliationMixin
from .live_support import _levels, _not_submitted, _tick_size, _venue_order

MIN_LIVE_SUBMIT_INTERVAL_SECONDS = 0.25


class LiveAdaptiveMakerVenue(LiveOrderReconciliationMixin):
    """Live WEEX Maker venue with signed positions and bounded order reconciliation."""

    def __init__(
        self,
        gateway: WeexGateway,
        symbol: str,
        position_side: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        strict_same_side_bbo: bool = True,
        market_data: Any | None = None,
        order_updates: Any | None = None,
    ) -> None:
        normalized_side = position_side.strip().lower()
        if normalized_side not in {"long", "short"}:
            raise ValidationError("live Maker position side must be long or short")
        self.gateway = gateway
        self.symbol = symbol.upper()
        self.position_side = normalized_side
        self.clock = clock
        self.sleep = sleep
        self.strict_same_side_bbo = strict_same_side_bbo
        self.market_data = market_data
        self.order_updates = order_updates
        self._progress_sink: ProgressSink | None = None
        self.trading = TradingService(gateway, sleep=sleep, retry_sink=self._on_retry)
        self._last_mid: float | None = None
        self._last_submit_at: float | None = None
        self._known_orders: dict[str, VenueOrder] = {}
        self._last_market_source: str | None = None

    def set_progress_sink(self, sink: ProgressSink | None) -> None:
        self._progress_sink = sink

    def _on_retry(self, event: Mapping[str, object]) -> None:
        if self._progress_sink is None:
            return
        delay_seconds = float(event.get("delay_seconds") or 0)
        self._progress_sink(
            {
                "event": "wait",
                "waiting_for": str(event.get("operation") or "exchange_read"),
                "next_check_ms": round(delay_seconds * 1000),
                "attempt": event.get("next_attempt"),
                "max_attempts": event.get("max_attempts"),
                "error": event.get("error"),
            }
        )

    @property
    def now_ms(self) -> int:
        return round(self.clock() * 1000)

    def snapshot(self) -> MarketSnapshot:
        source = "rest"
        if self.market_data is not None:
            try:
                book = self.market_data.order_book(self.symbol, 5)
                source = "websocket"
            except MarketStreamUnavailable:
                book = self.gateway.order_book(self.symbol, 5)
        else:
            book = self.gateway.order_book(self.symbol, 5)
        if source != self._last_market_source:
            self._last_market_source = source
            if self._progress_sink is not None:
                self._progress_sink({"event": "market_data_source", "source": source})
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
        active: list[tuple[str, Decimal]] = []
        for row in self.gateway.positions("live", self.symbol):
            quantity = Decimal(summarize_position_size(row))
            if quantity <= 0:
                continue
            info = row.get("info") if isinstance(row.get("info"), Mapping) else {}
            side = str(row.get("side") or info.get("positionSide") or info.get("side") or "").lower()
            active.append((side, quantity))
        if len(active) > 1:
            raise ValidationError(f"multiple active {self.symbol} positions found")
        if active and active[0][0] != self.position_side:
            raise ValidationError(f"unexpected {active[0][0] or 'unknown'} {self.symbol} position found")
        if not active:
            return 0.0
        quantity = float(active[0][1])
        return quantity if self.position_side == "long" else -quantity

    def wait_for_submission_slot(self) -> None:
        remaining_ms = self.submission_wait_ms()
        if remaining_ms > 0:
            self.sleep(remaining_ms / 1000)

    def submission_wait_ms(self) -> int:
        if self._last_submit_at is None:
            return 0
        remaining = MIN_LIVE_SUBMIT_INTERVAL_SECONDS - (self.clock() - self._last_submit_at)
        return max(0, math.ceil(remaining * 1000))

    def submit_post_only(self, side: Side, quantity: float, price: float, client_order_id: str) -> VenueOrder:
        self.wait_for_submission_slot()
        precise_quantity = retry_read(
            lambda: self.gateway.amount_to_precision(self.symbol, Decimal(str(quantity))),
            operation="amount_precision",
            policy=FAST_READ_RETRY_POLICY,
            sleep=self.sleep,
            retry_sink=self._on_retry,
        )
        precise_price = retry_read(
            lambda: self.gateway.price_to_precision(self.symbol, Decimal(str(price))),
            operation="price_precision",
            policy=FAST_READ_RETRY_POLICY,
            sleep=self.sleep,
            retry_sink=self._on_retry,
        )
        if precise_quantity <= 0:
            raise ValidationError("adaptive order quantity is below WEEX precision")
        if precise_price <= 0:
            raise ValidationError("adaptive order price is below WEEX precision")

        try:
            latest = retry_read(
                self.snapshot,
                operation="submission_book_check",
                policy=FAST_READ_RETRY_POLICY,
                sleep=self.sleep,
                retry_sink=self._on_retry,
            )
        except Exception as exc:  # noqa: BLE001 - no mutation occurred
            reason = f"LOCAL_BOOK_UNAVAILABLE:{type(exc).__name__}"
            return _not_submitted(side, client_order_id, precise_quantity, precise_price, reason)
        remains_post_only = (
            precise_price < Decimal(str(latest.ask)) if side == "buy" else precise_price > Decimal(str(latest.bid))
        )
        if not remains_post_only:
            return _not_submitted(side, client_order_id, precise_quantity, precise_price, "LOCAL_PRICE_WOULD_TAKE")
        same_side_bbo = Decimal(str(latest.bid if side == "buy" else latest.ask))
        if self.strict_same_side_bbo and precise_price != same_side_bbo:
            return _not_submitted(side, client_order_id, precise_quantity, precise_price, "LOCAL_NOT_SAME_SIDE_BBO")

        reduce_only = (self.position_side == "long" and side == "sell") or (
            self.position_side == "short" and side == "buy"
        )
        intent = OrderIntent.create(
            mode="live",
            symbol=self.symbol,
            side=side,
            position_side=self.position_side,
            order_type="limit",
            quantity=precise_quantity,
            price=precise_price,
            time_in_force="POST_ONLY",
            client_order_id=client_order_id,
            reduce_only=reduce_only,
        )
        # This event is the durable boundary between read-only preparation and
        # a call that may have created exchange-side state.  Emit it only after
        # every local POST_ONLY check has passed and immediately before submit.
        if self._progress_sink is not None:
            self._progress_sink(
                {
                    "event": "order_submission_attempted",
                    "symbol": self.symbol,
                    "side": side,
                    "position_side": self.position_side,
                    "reduce_only": reduce_only,
                }
            )
        self._last_submit_at = self.clock()
        submission = self.trading.submit_order(intent, allow_existing=True)
        raw = submission.get("result") or submission.get("order")
        if not isinstance(raw, Mapping):
            raise SubmissionUncertainError(
                "live submission returned no order for "
                f"client_order_id={client_order_id}; inspect orders before retrying"
            )
        order = _venue_order(
            raw,
            fallback_side=side,
            fallback_client_id=client_order_id,
            fallback_quantity=float(precise_quantity),
            fallback_price=float(precise_price),
        )
        if not order.order_id:
            raise SubmissionUncertainError(
                "live submission returned no order ID for "
                f"client_order_id={client_order_id}; inspect orders before retrying"
            )
        order = self._remember(order)
        if not order.post_only:
            verified = retry_read(
                lambda: self.fetch_order(order.order_id, order.client_order_id),
                operation="submission_post_only_verification",
                policy=FAST_READ_RETRY_POLICY,
                sleep=self.sleep,
                retry_sink=self._on_retry,
            )
            if not verified.post_only:
                raise SubmissionUncertainError(
                    "live order was accepted but POST_ONLY could not be verified for "
                    f"client_order_id={client_order_id}; inspect and cancel before continuing"
                )
            order = verified
        return order

    def advance(self, milliseconds: int) -> None:
        self.sleep(milliseconds / 1000)

    def _known_order(self, order_id: str, client_order_id: str) -> VenueOrder | None:
        return self._known_orders.get(order_id) or self._known_orders.get(client_order_id)

    def _remember(self, order: VenueOrder) -> VenueOrder:
        if order.order_id:
            self._known_orders[order.order_id] = order
        if order.client_order_id:
            self._known_orders[order.client_order_id] = order
        return order

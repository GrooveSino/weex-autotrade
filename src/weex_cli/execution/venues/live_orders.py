"""Bounded read-only order reconciliation for the live Maker venue."""

from __future__ import annotations

from collections.abc import Mapping

from weex_cli.core.errors import ValidationError
from weex_cli.core.reliability import NETWORK_ERRORS
from weex_cli.execution.adaptive import VenueOrder
from weex_cli.execution.adaptive_maker import Side

from .live_support import _row_count, _same_order, _venue_order

TIMEOUT_CLEANUP_VERIFY_ATTEMPTS = 5


class LiveOrderReconciliationMixin:
    def fetch_order(self, order_id: str, client_order_id: str) -> VenueOrder:
        cached = self._known_order(order_id, client_order_id)
        if self.order_updates is not None:
            pushed = self.order_updates.order_update(order_id, client_order_id)
            if isinstance(pushed, Mapping):
                fallback_side: Side = cached.side if cached is not None else "buy"
                return self._remember(
                    _venue_order(pushed, fallback_side=fallback_side, fallback_client_id=client_order_id)
                )
        successful_sources = 0
        last_network_error: Exception | None = None
        try:
            for row in self.gateway.open_orders(self.symbol, mode="live"):
                if isinstance(row, Mapping) and _same_order(row, order_id, client_order_id):
                    fallback_side: Side = cached.side if cached is not None else "buy"
                    order = _venue_order(row, fallback_side=fallback_side, fallback_client_id=client_order_id)
                    return self._remember(order)
            successful_sources += 1
        except NETWORK_ERRORS as exc:
            last_network_error = exc
        try:
            for row in self.gateway.order_history("live", self.symbol, limit=100):
                if isinstance(row, Mapping) and _same_order(row, order_id, client_order_id):
                    fallback_side = cached.side if cached is not None else "buy"
                    order = _venue_order(row, fallback_side=fallback_side, fallback_client_id=client_order_id)
                    return self._remember(order)
            successful_sources += 1
        except NETWORK_ERRORS as exc:
            last_network_error = exc
        if successful_sources == 0 and last_network_error is not None:
            raise last_network_error
        if cached is None:
            return VenueOrder(order_id, client_order_id, "buy", 0, 0, 0, 0, "unknown", True, None)
        return VenueOrder(
            cached.order_id,
            cached.client_order_id,
            cached.side,
            cached.price,
            cached.quantity,
            cached.filled_quantity,
            cached.cumulative_quote,
            "unknown",
            cached.post_only,
            cached.maker,
            cached.queue_ahead,
            "ORDER_NOT_VISIBLE",
        )

    def cancel_order(self, order_id: str, client_order_id: str) -> VenueOrder:
        cached = self._known_order(order_id, client_order_id)
        result = self.gateway.cancel_order(self.symbol, order_id, mode="live")
        if isinstance(result, Mapping):
            fallback_side: Side = cached.side if cached is not None else "buy"
            return self._remember(_venue_order(result, fallback_side=fallback_side, fallback_client_id=client_order_id))
        if cached is None:
            return VenueOrder(order_id, client_order_id, "buy", 0, 0, 0, 0, "unknown", True, None)
        return VenueOrder(
            cached.order_id,
            cached.client_order_id,
            cached.side,
            cached.price,
            cached.quantity,
            cached.filled_quantity,
            cached.cumulative_quote,
            "unknown",
            cached.post_only,
            cached.maker,
            cached.queue_ahead,
            "CANCEL_RESPONSE_UNCONFIRMED",
        )

    def cancel_all_and_verify(self, *, max_attempts: int = TIMEOUT_CLEANUP_VERIFY_ATTEMPTS) -> bool:
        """Cancel every live order for this symbol, then prove both order books are empty.

        The two cancellation requests are intentionally issued once each.  Any
        uncertainty is handled by bounded read-only verification; this method
        never resubmits a cancellation or places a replacement order.
        """
        if max_attempts < 1:
            raise ValidationError("timeout cleanup verification attempts must be positive")
        for trigger in (False, True):
            try:
                self.gateway.cancel_all_orders(self.symbol, mode="live", trigger=trigger)
            except NETWORK_ERRORS:
                # The request may have reached WEEX. Never submit it twice; verify below.
                continue
        for attempt in range(1, max_attempts + 1):
            try:
                regular = self.gateway.open_orders(self.symbol, mode="live", trigger=False)
                conditional = self.gateway.algo_orders(self.symbol)
            except NETWORK_ERRORS as exc:
                if attempt >= max_attempts:
                    return False
                delay = min(2.0, 0.25 * (2 ** (attempt - 1)))
                self._on_retry(
                    {
                        "operation": "cleanup_order_observation",
                        "next_attempt": attempt + 1,
                        "max_attempts": max_attempts,
                        "delay_seconds": delay,
                        "error": type(exc).__name__,
                    }
                )
                self.sleep(delay)
                continue
            if not regular and _row_count(conditional) == 0:
                return True
            if attempt < max_attempts:
                delay = min(2.0, 0.25 * (2 ** (attempt - 1)))
                self._on_retry(
                    {
                        "operation": "cleanup_order_clearance",
                        "next_attempt": attempt + 1,
                        "max_attempts": max_attempts,
                        "delay_seconds": delay,
                        "error": "OrdersStillVisible",
                    }
                )
                self.sleep(delay)
        return False

"""Demo Maker order observation and one-shot cancellation semantics."""

from __future__ import annotations

from collections.abc import Mapping

from weex_cli.core.errors import SubmissionUncertainError
from weex_cli.execution.adaptive import VenueOrder

from .demo_support import history_order, same_order, unknown_order, venue_order

OPEN_ORDER_QUERY_INTERVAL_SECONDS = 3.0


class DemoOrderReconciliationMixin:
    def fetch_order(self, order_id: str, client_order_id: str) -> VenueOrder:
        cached = self._known_order(order_id, client_order_id)
        rows = self.gateway.order_history("demo", None, limit=100)
        v3_canceled: VenueOrder | None = None
        for row in rows:
            if not same_order(row, order_id, client_order_id):
                continue
            order = venue_order(row)
            if order.status != "canceled" or order.cancellation_reason:
                return self._remember(order)
            v3_canceled = order

        if v3_canceled is not None:
            if self.clock() >= self._web_unavailable_until:
                try:
                    for row in self.gateway.demo_web_order_history(None, limit=100):
                        if same_order(row, order_id, client_order_id):
                            return self._remember(history_order(row))
                except Exception:  # noqa: BLE001 - V3 evidence remains the safe fallback
                    self._start_web_cooldown()
            return self._remember(unknown_order(v3_canceled, cancellation_reason="V3_CANCELED_REASON_UNKNOWN"))

        now = self.clock()
        if now < self._web_unavailable_until:
            if cached is not None:
                return unknown_order(cached, cancellation_reason="WEB_VISIBILITY_COOLDOWN")
            raise SubmissionUncertainError("WEEX Demo Web order visibility is cooling down after an error")
        if (
            cached is not None
            and self._last_open_query_at is not None
            and now - self._last_open_query_at < OPEN_ORDER_QUERY_INTERVAL_SECONDS
        ):
            return cached

        try:
            for row in self.gateway.open_orders(None, mode="demo"):
                if same_order(row, order_id, client_order_id):
                    self._last_open_query_at = self.clock()
                    return self._remember(venue_order(row))
        except Exception as exc:  # noqa: BLE001 - terminal history is a bounded read-only fallback
            self._start_web_cooldown()
            if cached is not None:
                return unknown_order(cached, cancellation_reason="WEB_VISIBILITY_COOLDOWN")
            raise SubmissionUncertainError(
                f"WEEX Demo open-order visibility is unavailable: {type(exc).__name__}"
            ) from exc
        self._last_open_query_at = self.clock()

        try:
            for row in self.gateway.demo_web_order_history(None, limit=100):
                if same_order(row, order_id, client_order_id):
                    return self._remember(history_order(row))
        except Exception:  # noqa: BLE001 - absence is still useful evidence
            self._start_web_cooldown()
        absent = cached or VenueOrder(order_id, client_order_id, "buy", 0, 0, 0, 0, "unknown", True, None)
        return self._remember(unknown_order(absent, cancellation_reason="OPEN_ORDER_ABSENT"))

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
            return self._remember(unknown_order(cached, cancellation_reason="OPEN_ORDER_ABSENT"))
        return self._remember(unknown_order(cached, cancellation_reason="CANCEL_UNCONFIRMED"))

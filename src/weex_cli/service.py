from __future__ import annotations

from collections.abc import Iterable
from contextlib import suppress
from typing import Any

import ccxt

from weex_cli.errors import SafetyError, SubmissionUncertainError
from weex_cli.gateway import WeexGateway, summarize_position_size
from weex_cli.models import OrderIntent
from weex_cli.redaction import redact_text


class TradingService:
    def __init__(self, gateway: WeexGateway) -> None:
        self.gateway = gateway

    def submit_order(self, intent: OrderIntent, *, allow_existing: bool = False) -> dict[str, Any]:
        precheck = self.precheck(intent, allow_existing=allow_existing)
        try:
            result = self.gateway.place_order(intent)
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            recovered = self.find_client_order(intent)
            if recovered:
                return {
                    "status": "recovered_after_submit_error",
                    "client_order_id": intent.client_order_id,
                    "precheck": precheck,
                    "order": recovered,
                    "warning": redact_text(exc),
                }
            raise SubmissionUncertainError(
                "submission outcome is unknown for "
                f"client_order_id={intent.client_order_id}; inspect orders before retrying: {redact_text(exc)}"
            ) from exc

        verification = self.verify_order(intent)
        return {
            "status": "submitted",
            "client_order_id": intent.client_order_id,
            "precheck": precheck,
            "result": result,
            "verification": verification,
        }

    def precheck(self, intent: OrderIntent, *, allow_existing: bool) -> dict[str, Any]:
        if intent.reduce_only or allow_existing:
            return {"status": "skipped", "reason": "reduce_only_or_explicit_override"}
        positions = self.gateway.positions(intent.mode, intent.symbol)
        active_positions = [row for row in positions if summarize_position_size(row) not in {"0", "None"}]
        open_orders: list[dict[str, Any]] = []
        if intent.mode == "live":
            open_orders = self.gateway.open_orders(intent.symbol)
        if active_positions or open_orders:
            raise SafetyError(
                "existing position or order detected; use --allow-existing only after reviewing account state"
            )
        return {
            "status": "clear",
            "active_positions": len(active_positions),
            "open_orders": len(open_orders),
        }

    def verify_order(self, intent: OrderIntent) -> dict[str, Any]:
        found = self.find_client_order(intent)
        positions = self.gateway.positions(intent.mode, intent.symbol)
        return {
            "order_found": found is not None,
            "order": found,
            "positions": positions,
        }

    def find_client_order(self, intent: OrderIntent) -> dict[str, Any] | None:
        rows: list[dict[str, Any]] = []
        if intent.mode == "live":
            with suppress(Exception):  # History remains a valid recovery source.
                rows.extend(self.gateway.open_orders(intent.symbol))
        with suppress(Exception):  # Preserve the original uncertain submit error.
            rows.extend(self.gateway.order_history(intent.mode, intent.symbol, limit=100))
        return _find_by_client_id(rows, intent.client_order_id)

    def place_bracket(
        self,
        *,
        symbol: str,
        position_side: str,
        take_profit: str,
        stop_loss: str,
        quantity: str,
        trigger_price_type: str,
        client_prefix: str,
    ) -> dict[str, Any]:
        # Protection is submitted first so a later TP failure never leaves the position unprotected.
        stop_result = self.gateway.place_tp_sl(
            symbol=symbol,
            plan_type="STOP_LOSS",
            trigger_price=stop_loss,
            position_side=position_side,
            client_algo_id=f"{client_prefix}-sl",
            quantity=quantity,
            trigger_price_type=trigger_price_type,
        )
        stop_verified = _find_by_client_id(self.gateway.algo_orders(symbol), f"{client_prefix}-sl")
        if stop_verified is None:
            raise SubmissionUncertainError(
                f"new stop {client_prefix}-sl was submitted but not found; inspect risk orders before retrying"
            )
        take_profit_result = self.gateway.place_tp_sl(
            symbol=symbol,
            plan_type="TAKE_PROFIT",
            trigger_price=take_profit,
            position_side=position_side,
            client_algo_id=f"{client_prefix}-tp",
            quantity=quantity,
            trigger_price_type=trigger_price_type,
        )
        return {
            "status": "submitted",
            "stop_loss": {"result": stop_result, "verified": stop_verified},
            "take_profit": {"result": take_profit_result},
            "open_algo_orders": self.gateway.algo_orders(symbol),
        }

    def replace_stop(
        self,
        *,
        symbol: str,
        old_order_id: str,
        trigger_price: str,
        position_side: str,
        quantity: str,
        trigger_price_type: str,
        client_algo_id: str,
    ) -> dict[str, Any]:
        new_result = self.gateway.place_tp_sl(
            symbol=symbol,
            plan_type="STOP_LOSS",
            trigger_price=trigger_price,
            position_side=position_side,
            client_algo_id=client_algo_id,
            quantity=quantity,
            trigger_price_type=trigger_price_type,
        )
        current = self.gateway.algo_orders(symbol)
        verified = _find_by_client_id(current, client_algo_id)
        if verified is None:
            raise SubmissionUncertainError(
                f"replacement stop {client_algo_id} was submitted but not found; old stop was left untouched"
            )
        cancel_result = self.gateway.cancel_algo_order(old_order_id)
        return {
            "status": "replaced",
            "new_stop": {"result": new_result, "verified": verified},
            "old_stop_cancel": cancel_result,
            "open_algo_orders": self.gateway.algo_orders(symbol),
        }


def _find_by_client_id(rows: Iterable[Any], client_order_id: str) -> dict[str, Any] | None:
    for row in rows:
        if not isinstance(row, dict):
            continue
        values = (
            row.get("clientOrderId"),
            row.get("client_order_id"),
            row.get("clientAlgoId"),
            (row.get("info") or {}).get("clientOrderId") if isinstance(row.get("info"), dict) else None,
        )
        if client_order_id in {str(value) for value in values if value is not None}:
            return row
    return None

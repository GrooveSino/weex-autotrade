"""Execution result, checkpoint, and event-projection behavior."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from weex_cli.core.models import decimal_text

from .support import active_positions, row_count


class LiveMakerVolumeLifecycleMixin:
    def _final_acceptance(self) -> dict[str, Any]:
        assert self.plan is not None
        try:
            flat = not active_positions(self.gateway, self.plan.symbol)
            no_regular = not self.gateway.open_orders(self.plan.symbol, mode="live")
            no_triggers = row_count(self.gateway.algo_orders(self.plan.symbol)) == 0
        except Exception as exc:  # noqa: BLE001 - final state must be observed, never assumed
            return self._finish("uncertain", f"final_observation:{type(exc).__name__.lower()}")
        completed = (
            self.verified_quote >= self.plan.target_quote
            and flat
            and no_regular
            and no_triggers
            and self.taker_count == 0
            and self.unknown_liquidity_count == 0
        )
        return self._finish(
            "completed" if completed else "uncertain",
            "maker_volume_target_completed" if completed else "final_acceptance_invariant_failed",
        )

    def _finish(self, status: str, reason: str) -> dict[str, Any]:
        assert self.plan is not None
        reconciliation_required = status == "uncertain"
        payload = self._result(status, reason, reconciliation_required=reconciliation_required)
        self._emit(
            "volume_workflow_finished",
            status=status,
            reason=reason,
            verified_quote=decimal_text(self.verified_quote),
        )
        payload["timeline"] = list(self.timeline)
        self.store.save(self.plan, state=status, result=payload)
        return payload

    def _result(self, status: str, reason: str, *, reconciliation_required: bool) -> dict[str, Any]:
        assert self.plan is not None
        remaining = max(Decimal(0), self.plan.target_quote - self.verified_quote)
        excess = max(Decimal(0), self.verified_quote - self.plan.target_quote)
        return {
            "schema_version": 1,
            "kind": "live_maker_volume_execution",
            "mode": "live",
            "status": status,
            "reason": reason,
            "plan_id": self.plan.plan_id,
            "symbol": self.plan.symbol,
            "target_quote": decimal_text(self.plan.target_quote),
            "verified_quote": decimal_text(self.verified_quote),
            "remaining_quote": decimal_text(remaining),
            "excess_quote": decimal_text(excess),
            "achievement_percent": decimal_text(self.verified_quote / self.plan.target_quote * 100),
            "rounds_completed": sum(1 for row in self.rounds if row.get("flat")),
            "rounds_attempted": len(self.rounds),
            "fill_count": self.fill_count,
            "maker_count": self.maker_count,
            "taker_count": self.taker_count,
            "unknown_liquidity_count": self.unknown_liquidity_count,
            "maker_only": self.fill_count > 0 and self.taker_count == 0 and self.unknown_liquidity_count == 0,
            "commission_by_asset": {
                asset: decimal_text(amount) for asset, amount in sorted(self.commission_by_asset.items())
            },
            "realized_pnl": decimal_text(self.realized_pnl),
            "elapsed_ms": max(0, self.now_ms() - self.started_at_ms),
            "rounds": list(self.rounds),
            "reconciliation_required": reconciliation_required,
            "retry_allowed": False,
            "recovery": (
                "Inspect live positions and active orders before creating a new plan."
                if reconciliation_required
                else None
            ),
            "timeline": list(self.timeline),
        }

    def _checkpoint(self, state: str, reason: str, **fields: Any) -> None:
        assert self.plan is not None
        payload = self._result(state, reason, reconciliation_required=False)
        payload.update(fields)
        self.store.save(self.plan, state=state, result=payload)

    def _reset(self, plan: Any) -> None:
        self.plan = plan
        self.timeline = []
        self.rounds = []
        self.verified_quote = Decimal(0)
        self.maker_count = 0
        self.taker_count = 0
        self.unknown_liquidity_count = 0
        self.fill_count = 0
        self.commission_by_asset = defaultdict(Decimal)
        self.realized_pnl = Decimal(0)
        self.started_at_ms = self.now_ms()

    def _emit(self, event: str, **fields: Any) -> None:
        row = {
            "event_index": len(self.timeline) + 1,
            "event": event,
            "plan_id": self.plan.plan_id if self.plan is not None else None,
            "timestamp_ms": self.now_ms(),
            **fields,
        }
        self.timeline.append(row)
        if self.event_sink is None:
            return
        try:
            self.event_sink(row)
        except Exception:  # noqa: BLE001 - progress presentation must not alter execution
            return

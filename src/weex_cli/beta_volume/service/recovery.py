from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

from weex_cli.core.errors import SafetyError, ValidationError

from ..accounting.fills import (
    accounting_summary,
)
from ..accounting.termination import is_uncertain_stop
from ..contracts import (
    ExecutionLane,
)
from ..plan import BetaVolumePlan
from ..safety import (
    _row_count,
    observed_recovery_quantity,
)


class RecoveryMixin:
    def recover(self, plan: BetaVolumePlan, symbol: str, quantity: Decimal) -> dict[str, Any]:
        normalized_symbol = symbol.upper()
        if normalized_symbol not in {"BTC", "ETH"}:
            raise ValidationError("recovery symbol must be BTC or ETH")
        leg_plan = plan.btc if normalized_symbol == "BTC" else plan.eth
        position_side = leg_plan.position_side
        current = observed_recovery_quantity(self.gateway, normalized_symbol, position_side)
        if current <= leg_plan.amount_step / 2:
            return {
                "schema_version": 1,
                "kind": "beta_volume_recovery",
                "mode": "live",
                "status": "completed",
                "reason": "already_flat",
                "plan_id": plan.plan_id,
                "symbol": normalized_symbol,
                "position_side": position_side,
                "maker_only": True,
                "executed_quote_volume": "0",
                "final_position": "0",
                "reconciliation_required": False,
            }
        if abs(current - quantity) > leg_plan.amount_step / 2:
            raise SafetyError("recovery quantity changed since dry run; create a new recovery dry run")
        if self.gateway.open_orders(normalized_symbol, mode="live") or _row_count(
            self.gateway.algo_orders(normalized_symbol)
        ):
            raise SafetyError("recovery requires no active regular or trigger orders")
        venue = self._create_venue(self.gateway, normalized_symbol, position_side)
        lane = ExecutionLane(self.gateway, venue, self.reconciler_factory(self.gateway))
        close_plan = replace(leg_plan, quantity=quantity, allocated_quote=Decimal(0))
        summaries, _, stop = self._flatten_lane(plan, 1, 1, close_plan, lane)
        final_position = self._observe_position(
            venue,
            round_number=1,
            sequence="recovery",
            symbol=normalized_symbol,
            action="close",
        )
        accounting = accounting_summary(summaries)
        flat = final_position is not None and abs(Decimal(str(final_position))) <= leg_plan.amount_step / 2
        no_orders = (
            not self.gateway.open_orders(normalized_symbol, mode="live")
            and _row_count(self.gateway.algo_orders(normalized_symbol)) == 0
        )
        completed = flat and no_orders and accounting["verified"] and accounting["maker_only"] and stop is None
        status = "completed" if completed else "uncertain" if stop and is_uncertain_stop(stop) else "stopped"
        result = {
            "schema_version": 1,
            "kind": "beta_volume_recovery",
            "mode": "live",
            "status": status,
            "reason": "maker_recovery_completed" if completed else (stop[1] if stop else "recovery_invariant_failed"),
            "plan_id": plan.plan_id,
            "symbol": normalized_symbol,
            "position_side": position_side,
            "maker_only": accounting["maker_only"],
            "executed_quote_volume": accounting["executed_quote_volume"],
            "accounting": accounting,
            "legs": summaries,
            "final_position": final_position,
            "reconciliation_required": not completed,
            "retry_allowed": False,
        }
        self.store.save_recovery(plan, result, normalized_symbol)
        return result

    def cleanup(self, plan: BetaVolumePlan) -> dict[str, Any]:
        """Run the existing single-pass safe-stop convergence for a persisted plan."""
        lanes = self._create_lanes(plan)
        return self._safe_stop(
            plan,
            lanes,
            {},
            self.now_ms(),
            summaries=[],
            cycles=[],
            total_quote=Decimal(0),
            round_number=1,
        )

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

from ...accounting.fills import (
    _dust_close_summary,
)
from ...accounting.termination import is_hard_terminal, is_uncertain_stop
from ...contracts import (
    CycleLegSpec,
    ExecutionLane,
    PairLegPlan,
)
from ...plan import BetaVolumePlan
from ..hooks import close_dust_position


class PositionFlatteningMixin:
    def _flatten_lane(
        self,
        plan: BetaVolumePlan,
        round_number: int,
        sequence_offset: int,
        leg_plan: PairLegPlan,
        lane: ExecutionLane,
        *,
        respect_stop: bool = False,
        owned_quantity: Decimal | None = None,
    ) -> tuple[list[dict[str, Any]], bool, tuple[str, str] | None]:
        summaries: list[dict[str, Any]] = []
        for attempt in range(1, plan.recovery_attempts + 1):
            position = self._observe_position(
                lane.venue,
                round_number=round_number,
                sequence=f"recovery-{attempt}",
                symbol=leg_plan.symbol,
                action="close",
            )
            if position is None:
                return summaries, False, ("observation_uncertain", "position_observation_unavailable")
            quantity = abs(Decimal(str(position)))
            if quantity <= leg_plan.amount_step / 2:
                return summaries, True, None
            pre_maker_dust = self._close_dust_if_eligible(
                plan,
                round_number,
                leg_plan,
                lane,
                owned_quantity,
                "below_minimum",
                sequence_offset + 900 + attempt,
            )
            if pre_maker_dust is not None:
                summary, flat, dust_stop = pre_maker_dust
                if summary is not None:
                    summaries.append(summary)
                return summaries, flat, dust_stop
            close_plan = replace(leg_plan, quantity=quantity, allocated_quote=Decimal(0))
            spec = CycleLegSpec(
                close_plan,
                "close",
                leg_plan.closing_side,
                0.0,
                f"{plan.plan_id}-r{round_number:03d}-{leg_plan.symbol.lower()}c{attempt}",
            )
            summary, stop = self._execute_leg(
                plan,
                (round_number - 1) * (4 + plan.recovery_attempts * 2) + sequence_offset + (attempt - 1) * 2,
                spec,
                lane,
                round_number,
                respect_stop=respect_stop,
            )
            summary["recovery_attempt"] = attempt
            summaries.append(summary)
            position = self._observe_position(
                lane.venue,
                round_number=round_number,
                sequence=f"recovery-{attempt}",
                symbol=leg_plan.symbol,
                action="close_check",
            )
            if position is not None and abs(Decimal(str(position))) <= leg_plan.amount_step / 2:
                return summaries, True, stop
            maker_reason = stop[1] if stop is not None else "maker_completed_with_residual"
            dust = self._close_dust_if_eligible(
                plan,
                round_number,
                leg_plan,
                lane,
                owned_quantity,
                maker_reason,
                sequence_offset + 950 + attempt,
            )
            if dust is not None:
                dust_summary, flat, dust_stop = dust
                if dust_summary is not None:
                    summaries.append(dust_summary)
                return summaries, flat, dust_stop
            if stop is not None and (is_uncertain_stop(stop) or is_hard_terminal(stop[1])):
                return summaries, False, stop
        return summaries, False, ("stopped", "recovery_attempts_exhausted")

    def _close_dust_if_eligible(
        self,
        plan: BetaVolumePlan,
        round_number: int,
        leg_plan: PairLegPlan,
        lane: ExecutionLane,
        owned_quantity: Decimal | None,
        maker_reason: str,
        sequence: int,
    ) -> tuple[dict[str, Any] | None, bool, tuple[str, str] | None] | None:
        if plan.schema_version < 5 or owned_quantity is None or owned_quantity <= 0:
            return None
        result = close_dust_position(
            gateway=lane.gateway,
            store=self.store,
            plan=plan,
            cycle=round_number,
            symbol=leg_plan.symbol,
            position_side=leg_plan.position_side,
            owned_quantity=owned_quantity,
            amount_step=leg_plan.amount_step,
            maker_reason=maker_reason,
            reconciler=lane.reconciler,
            now_ms=self.now_ms,
            sleep=self.sleep,
            emit=self._emit,
        )
        # The lane venue is the phase-local source used by the Maker executor.
        # A separate gateway read that appears flat cannot override the non-flat
        # lane observation that led us here unless closePositions was attempted.
        if not result.attempted and not result.uncertain:
            return None
        summary = _dust_close_summary(sequence, leg_plan, result) if result.attempted else None
        if result.uncertain:
            return summary, False, ("submission_uncertain", result.reason)
        if result.flat and result.reason == "audit_pending":
            return summary, True, ("stopped", "dust_close_audit_pending")
        return summary, result.flat, None

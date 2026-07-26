from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from weex_cli.core.models import decimal_text
from weex_cli.execution.adaptive import (
    ObservationUnavailableError,
    TargetRequest,
)
from weex_cli.execution.adaptive_maker import AdaptiveMakerPolicy
from weex_cli.execution.adaptive_volume import REAL_POLICY
from weex_cli.execution.dust_position_close import (
    classify_minimum_order_rejection,
)

from ...accounting.fills import (
    _leg_exception_summary,
)
from ...contracts import (
    CycleLegSpec,
    ExecutionLane,
)
from ...plan import BetaVolumePlan
from ..hooks import execute_maker_target


class LegExecutionMixin:
    def _execute_leg(
        self,
        plan: BetaVolumePlan,
        sequence: int,
        spec: CycleLegSpec,
        lane: ExecutionLane,
        round_number: int,
        *,
        respect_stop: bool = True,
    ) -> tuple[dict[str, Any], tuple[str, str] | None]:
        venue = lane.venue
        started_at_ms = self.now_ms()
        self._emit(
            "leg_preparing",
            round=round_number,
            sequence=sequence,
            symbol=spec.plan.symbol,
            action=spec.action,
            side=spec.side,
        )
        start_position = self._observe_position(
            venue,
            round_number=round_number,
            sequence=sequence,
            symbol=spec.plan.symbol,
            action=f"{spec.action}_start",
        )
        if start_position is None:
            reason = "starting_position_unavailable"
            return _leg_exception_summary(sequence, spec, reason), ("observation_uncertain", reason)
        self._emit(
            "leg_started",
            round=round_number,
            sequence=sequence,
            symbol=spec.plan.symbol,
            action=spec.action,
            side=spec.side,
            quantity=decimal_text(spec.plan.quantity),
        )

        def progress_sink(event: Mapping[str, object]) -> None:
            detail = dict(event)
            progress_event = str(detail.pop("event", "unknown"))
            self._emit(
                "leg_progress",
                round=round_number,
                sequence=sequence,
                symbol=spec.plan.symbol,
                action=spec.action,
                side=spec.side,
                progress_event=progress_event,
                **detail,
            )

        try:
            executor_kwargs: dict[str, Any] = {"progress_sink": progress_sink}
            if respect_stop and self._stop_callback_configured:
                executor_kwargs["stop_requested"] = self.stop_requested
            result = execute_maker_target(
                venue,
                AdaptiveMakerPolicy(REAL_POLICY),
                TargetRequest(
                    side=spec.side,  # type: ignore[arg-type]
                    target_position=spec.target_position,
                    deadline_ms=plan.timeout_seconds * 1000,
                    poll_interval_ms=250,
                    max_requotes=max(30, plan.timeout_seconds),
                    tolerance_quantity=float(spec.plan.amount_step / 2),
                    client_prefix=spec.client_prefix,
                ),
                **executor_kwargs,
            )
        except ObservationUnavailableError as exc:
            reason = exc.reason
            summary = _leg_exception_summary(sequence, spec, reason)
            self._emit(
                "leg_uncertain",
                round=round_number,
                sequence=sequence,
                symbol=spec.plan.symbol,
                action=spec.action,
                reason=reason,
            )
            return summary, ("observation_uncertain", reason)
        except Exception as exc:  # noqa: BLE001 - a mutation may have landed; never continue to another leg
            minimum_reason = classify_minimum_order_rejection(exc)
            if minimum_reason is not None:
                summary = _leg_exception_summary(sequence, spec, minimum_reason)
                self._emit(
                    "leg_stopped",
                    round=round_number,
                    sequence=sequence,
                    symbol=spec.plan.symbol,
                    action=spec.action,
                    reason=minimum_reason,
                )
                return summary, ("stopped", minimum_reason)
            reason = f"leg_exception:{type(exc).__name__.lower()}"
            summary = _leg_exception_summary(sequence, spec, reason)
            deadline_reached = self.now_ms() - started_at_ms >= plan.timeout_seconds * 1000
            cleanup = getattr(venue, "cancel_all_and_verify", None)
            if deadline_reached and callable(cleanup):
                self._emit(
                    "leg_progress",
                    round=round_number,
                    sequence=sequence,
                    symbol=spec.plan.symbol,
                    action=spec.action,
                    side=spec.side,
                    progress_event="timeout_cleanup_started",
                )
                try:
                    cleanup_confirmed = bool(cleanup())
                except Exception as cleanup_exc:  # noqa: BLE001 - timeout cleanup must fail closed
                    cleanup_confirmed = False
                    reason = "deadline_cleanup_not_confirmed"
                    self._emit(
                        "leg_progress",
                        round=round_number,
                        sequence=sequence,
                        symbol=spec.plan.symbol,
                        action=spec.action,
                        side=spec.side,
                        progress_event="timeout_cleanup_error",
                        error=type(cleanup_exc).__name__,
                    )
                if not cleanup_confirmed:
                    reason = "deadline_cleanup_not_confirmed"
                    self._emit(
                        "leg_progress",
                        round=round_number,
                        sequence=sequence,
                        symbol=spec.plan.symbol,
                        action=spec.action,
                        side=spec.side,
                        progress_event="timeout_cleanup_not_confirmed",
                    )
                    summary = _leg_exception_summary(sequence, spec, reason)
                    self._emit(
                        "leg_uncertain",
                        round=round_number,
                        sequence=sequence,
                        symbol=spec.plan.symbol,
                        action=spec.action,
                        reason=reason,
                    )
                    return summary, ("submission_uncertain", reason)
                self._emit(
                    "leg_progress",
                    round=round_number,
                    sequence=sequence,
                    symbol=spec.plan.symbol,
                    action=spec.action,
                    side=spec.side,
                    progress_event="timeout_cleanup_confirmed",
                )
            self._emit(
                "leg_stopped" if deadline_reached and callable(cleanup) else "leg_uncertain",
                round=round_number,
                sequence=sequence,
                symbol=spec.plan.symbol,
                action=spec.action,
                reason=reason,
            )
            return summary, ("stopped" if deadline_reached and callable(cleanup) else "submission_uncertain", reason)

        return self._reconcile_leg_result(
            plan,
            sequence,
            spec,
            lane,
            round_number,
            venue,
            start_position,
            started_at_ms,
            result,
        )

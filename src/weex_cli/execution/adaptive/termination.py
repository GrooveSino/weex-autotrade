from __future__ import annotations

from .contracts import ObservationUnavailableError, TargetExecutionResult, VenueOrder
from .runtime import ExecutionRuntime


class TerminationController:
    def __init__(self, runtime: ExecutionRuntime) -> None:
        self.runtime = runtime

    def cancel_and_verify(self, order: VenueOrder, *, capture_position: bool = True) -> VenueOrder | None:
        runtime = self.runtime
        state = runtime.state
        position_before_cancel: float | None = None
        if capture_position:
            try:
                position_before_cancel = runtime.venue.position_quantity()
            except Exception:  # noqa: BLE001 - position proof is optional; do not delay the single cancel request
                position_before_cancel = None
        response: VenueOrder | None = None
        last_verified: VenueOrder | None = None
        runtime.record({"event": "cancel_started", "order_id": order.order_id})

        try:
            response = runtime.venue.cancel_order(order.order_id, order.client_order_id)
            runtime.record({"event": "cancel_response", "status": response.status, "order_id": order.order_id})
        except Exception as exc:  # noqa: BLE001 - cancel may have landed; only verify afterward
            state.cancel_verification_errors += 1
            runtime.record({"event": "cancel_request_error", "error": type(exc).__name__, "order_id": order.order_id})

        for attempt in range(1, runtime.request.max_cancel_verification_attempts + 1):
            state.cancel_verification_attempts += 1
            try:
                verified = runtime.venue.fetch_order(order.order_id, order.client_order_id)
            except Exception as exc:  # noqa: BLE001 - bounded read-only verification after one cancel request
                state.cancel_verification_errors += 1
                runtime.record(
                    {
                        "event": "cancel_verification_error",
                        "attempt": attempt,
                        "error": type(exc).__name__,
                        "order_id": order.order_id,
                    }
                )
            else:
                last_verified = verified
                runtime.record(
                    {
                        "event": "cancel_verification",
                        "attempt": attempt,
                        "status": verified.status,
                        "order_id": order.order_id,
                    }
                )
                if verified.status in {"filled", "canceled"}:
                    return verified
                reconciled = self._reconcile_absent(response, position_before_cancel, verified, attempt)
                if reconciled is not None:
                    return reconciled
            if attempt < runtime.request.max_cancel_verification_attempts:
                delay = min(2_000, max(250, runtime.request.poll_interval_ms) * (2 ** (attempt - 1)))
                runtime.record_wait(
                    "cancel_confirmation",
                    delay,
                    force=True,
                    order_id=order.order_id,
                    attempt=attempt + 1,
                    max_attempts=runtime.request.max_cancel_verification_attempts,
                )
                runtime.venue.advance(delay)
        if last_verified is None:
            return None
        return self._reconcile_absent(
            response, position_before_cancel, last_verified, runtime.request.max_cancel_verification_attempts
        )

    def _reconcile_absent(
        self,
        response: VenueOrder | None,
        position_before_cancel: float | None,
        verified: VenueOrder,
        attempt: int,
    ) -> VenueOrder | None:
        runtime = self.runtime
        if (
            response is None
            or response.cancellation_reason != "OPEN_ORDER_ABSENT"
            or verified.status != "unknown"
            or verified.cancellation_reason
            not in {"OPEN_ORDER_ABSENT", "V3_CANCELED_REASON_UNKNOWN", "CANCELED_REASON_UNKNOWN"}
            or position_before_cancel is None
        ):
            return None
        try:
            position_after_cancel = runtime.read_position(order_id=verified.order_id)
        except Exception as exc:  # noqa: BLE001 - leave cancellation uncertain when position cannot be checked
            runtime.state.cancel_verification_errors += 1
            runtime.record(
                {
                    "event": "cancel_position_verification_error",
                    "error": type(exc).__name__,
                    "order_id": verified.order_id,
                }
            )
            return None
        if abs(position_after_cancel - position_before_cancel) > runtime.request.tolerance_quantity:
            return None
        runtime.record({"event": "cancel_reconciled_absent", "attempts": attempt, "order_id": verified.order_id})
        return VenueOrder(
            response.order_id,
            response.client_order_id,
            response.side,
            response.price,
            response.quantity,
            response.filled_quantity,
            response.cumulative_quote,
            "canceled",
            response.post_only,
            response.maker,
            response.queue_ahead,
            response.cancellation_reason,
        )

    def stop_after_observation_failure(self, reason: str) -> TargetExecutionResult:
        runtime = self.runtime
        state = runtime.state
        if state.active is None:
            return runtime.finish("uncertain", reason)
        runtime.record({"event": "observation_cleanup_started", "reason": reason, "order_id": state.active.order_id})
        verified = self.cancel_and_verify(state.active, capture_position=reason != "position_observation_unavailable")
        if verified is None:
            runtime.record(
                {"event": "observation_cleanup_not_confirmed", "reason": reason, "order_id": state.active.order_id}
            )
            return runtime.finish("uncertain", "cancel_not_confirmed")
        observation_error = runtime.observe(verified)
        if observation_error is not None:
            return runtime.finish("failed", observation_error)
        if verified.status == "canceled" and verified.cancellation_reason == "COULD_NOT_FILL":
            state.post_only_rejections += 1
            state.venue_cancels += 1
            return runtime.finish("failed", "post_only_rejected")
        if verified.status not in {"filled", "canceled"}:
            return runtime.finish("uncertain", "cancel_not_confirmed")
        if verified.status == "canceled":
            state.cancels += 1
        state.active = None
        runtime.record({"event": "observation_cleanup_confirmed", "reason": reason, "order_id": verified.order_id})
        try:
            final_position = runtime.read_position(order_id=verified.order_id)
        except ObservationUnavailableError:
            final_position = None
        return runtime.finish("uncertain", reason, final_position=final_position)

    def finish_deadline(self) -> TargetExecutionResult:
        runtime = self.runtime
        state = runtime.state
        cleanup = getattr(runtime.venue, "cancel_all_and_verify", None)
        if callable(cleanup):
            runtime.record({"event": "timeout_cleanup_started"})
            try:
                verified_empty = bool(cleanup())
            except Exception as exc:  # noqa: BLE001 - cleanup uncertainty is terminal
                runtime.record({"event": "timeout_cleanup_error", "error": type(exc).__name__})
                return runtime.finish("uncertain", "deadline_cleanup_not_confirmed")
            if not verified_empty:
                runtime.record({"event": "timeout_cleanup_not_confirmed"})
                return runtime.finish("uncertain", "deadline_cleanup_not_confirmed")
            runtime.record({"event": "timeout_cleanup_confirmed"})
            if state.active is not None:
                try:
                    verified = runtime.venue.fetch_order(state.active.order_id, state.active.client_order_id)
                except Exception as exc:  # noqa: BLE001 - read-only reconciliation
                    runtime.record(
                        {
                            "event": "timeout_order_verification_error",
                            "error": type(exc).__name__,
                            "order_id": state.active.order_id,
                        }
                    )
                    return runtime.finish("uncertain", "deadline_order_not_confirmed")
                observation_error = runtime.observe(verified)
                if observation_error is not None:
                    return runtime.finish("failed", observation_error)
                if verified.status not in {"filled", "canceled"}:
                    runtime.record(
                        {
                            "event": "timeout_order_not_confirmed",
                            "order_id": state.active.order_id,
                            "status": verified.status,
                        }
                    )
                    return runtime.finish("uncertain", "deadline_order_not_confirmed")
                state.active = None
        elif state.active is not None:
            verified = self.cancel_and_verify(state.active)
            if verified is None:
                return runtime.finish("uncertain", "deadline_cancel_not_confirmed")
            observation_error = runtime.observe(verified)
            if observation_error is not None:
                return runtime.finish("failed", observation_error)
            if verified.status == "canceled" and verified.cancellation_reason == "COULD_NOT_FILL":
                state.post_only_rejections += 1
                state.venue_cancels += 1
                return runtime.finish("failed", "post_only_rejected")
            state.cancels += 1
            state.active = None
        try:
            final_position = runtime.read_position()
        except ObservationUnavailableError:
            return runtime.finish("uncertain", "position_observation_unavailable")
        if abs(runtime.request.target_position - final_position) <= runtime.request.tolerance_quantity:
            return runtime.finish("completed", "target_reached", final_position=final_position)
        return runtime.finish("failed", "deadline_exceeded")

    def stop_for_request(self) -> TargetExecutionResult:
        runtime = self.runtime
        state = runtime.state
        runtime.record({"event": "stop_requested", "order_id": state.active.order_id if state.active else None})
        if state.active is not None:
            verified = self.cancel_and_verify(state.active)
            if verified is None:
                runtime.record({"event": "stop_cancel_not_confirmed", "order_id": state.active.order_id})
                return runtime.finish("uncertain", "stop_cancel_not_confirmed")
            observation_error = runtime.observe(verified)
            if observation_error is not None:
                return runtime.finish("failed", observation_error)
            if verified.status == "canceled" and verified.cancellation_reason == "COULD_NOT_FILL":
                state.post_only_rejections += 1
                state.venue_cancels += 1
                return runtime.finish("failed", "post_only_rejected")
            if verified.status not in {"filled", "canceled"}:
                runtime.record({"event": "stop_cancel_not_confirmed", "order_id": state.active.order_id})
                return runtime.finish("uncertain", "stop_cancel_not_confirmed")
            if verified.status == "canceled":
                state.cancels += 1
            state.active = None
        try:
            final_position = runtime.read_position()
        except ObservationUnavailableError:
            return runtime.finish("uncertain", "stop_position_observation_unavailable")
        runtime.record({"event": "stop_contained", "final_position": final_position})
        return runtime.finish("stopped", "stop_requested", final_position=final_position)

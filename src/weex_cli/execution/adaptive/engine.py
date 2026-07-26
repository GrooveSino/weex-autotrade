from __future__ import annotations

from collections.abc import Callable

from weex_cli.execution.adaptive_maker import MakerPolicy, MarketSnapshot, Side, WorkingQuote

from .contracts import MakerVenue, ObservationUnavailableError, ProgressSink, TargetExecutionResult, TargetRequest
from .runtime import ExecutionRuntime
from .termination import TerminationController


def execute_adaptive_maker_target(
    venue: MakerVenue,
    policy: MakerPolicy,
    request: TargetRequest,
    *,
    progress_sink: ProgressSink | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> TargetExecutionResult:
    runtime = ExecutionRuntime(venue, request, progress_sink, stop_requested)
    runtime.bind_venue_progress()
    runtime.state.start_position = runtime.read_position()
    return _run_adaptive_loop(runtime, policy)


def _run_adaptive_loop(runtime: ExecutionRuntime, policy: MakerPolicy) -> TargetExecutionResult:
    terminator = TerminationController(runtime)
    venue, request, state = runtime.venue, runtime.request, runtime.state
    while venue.now_ms - runtime.started_ms <= request.deadline_ms:
        if runtime.should_stop():
            return terminator.stop_for_request()
        try:
            current = runtime.read_position(order_id=state.active.order_id if state.active else None)
        except ObservationUnavailableError as exc:
            return terminator.stop_after_observation_failure(exc.reason)
        if request.side == "buy" and current > request.target_position + request.tolerance_quantity:
            return runtime.finish("failed", "target_overfilled")
        if request.side == "sell" and current < request.target_position - request.tolerance_quantity:
            return runtime.finish("failed", "target_overfilled")
        remaining = abs(request.target_position - current)

        if state.active is not None:
            outcome = _advance_active_order(runtime, policy, terminator, remaining)
            if outcome is not None:
                return outcome
            continue
        if remaining <= request.tolerance_quantity:
            return runtime.finish("completed", "target_reached")
        if runtime.should_stop():
            return terminator.stop_for_request()
        outcome = _submit_next_order(runtime, policy, terminator, remaining)
        if outcome is not None:
            return outcome
    return terminator.finish_deadline()


def _advance_active_order(
    runtime: ExecutionRuntime,
    policy: MakerPolicy,
    terminator: TerminationController,
    remaining: float,
) -> TargetExecutionResult | None:
    venue, request, state = runtime.venue, runtime.request, runtime.state
    active = state.active
    assert active is not None
    if runtime.should_stop():
        return terminator.stop_for_request()
    try:
        order = venue.fetch_order(active.order_id, active.client_order_id)
    except Exception as exc:  # noqa: BLE001 - retry only this read-only observation
        return _retry_order_observation(runtime, terminator, active.order_id, error=type(exc).__name__)
    state.active = order
    if order.status == "unknown":
        return _retry_order_observation(runtime, terminator, order.order_id, reason=order.cancellation_reason)
    state.consecutive_observation_errors = 0
    observation_error = runtime.observe(order)
    if observation_error is not None:
        return runtime.finish("failed", observation_error)
    if order.status == "rejected":
        if order.post_only:
            state.post_only_rejections += 1
        return runtime.finish("failed", "post_only_rejected")
    if order.status == "canceled" and order.cancellation_reason == "COULD_NOT_FILL":
        state.post_only_rejections += 1
        state.venue_cancels += 1
        runtime.record(
            {"event": "post_only_rejection", "order_id": order.order_id, "reason": order.cancellation_reason}
        )
        return runtime.finish("failed", "post_only_rejected")
    if order.status in {"filled", "canceled"}:
        if order.status == "canceled":
            state.venue_cancels += 1
        runtime.record({"event": "order_terminal", "status": order.status, "order_id": order.order_id})
        state.active = None
        state.last_filled = 0.0
        state.last_quote = 0.0
        return None
    try:
        snapshot = runtime.read_snapshot(order_id=order.order_id)
    except ObservationUnavailableError as exc:
        return terminator.stop_after_observation_failure(exc.reason)
    urgency = min(1.0, (venue.now_ms - runtime.started_ms) / request.deadline_ms)
    working = WorkingQuote(
        side=order.side,
        price=order.price,
        submitted_ms=state.active_submitted_ms if state.active_submitted_ms is not None else venue.now_ms,
        queue_ahead=order.queue_ahead,
        remaining_quantity=max(0.0, order.quantity - order.filled_quantity),
    )
    decision = policy.decide(snapshot, request.side, max(remaining, request.tolerance_quantity), urgency, working)
    if decision.action != "cancel":
        runtime.record_wait(
            "maker_fill",
            request.poll_interval_ms,
            order_id=order.order_id,
            status=order.status,
            filled_quantity=order.filled_quantity,
            order_quantity=order.quantity,
            remaining_quantity=remaining,
        )
        venue.advance(request.poll_interval_ms)
        return None
    verified = terminator.cancel_and_verify(order)
    if verified is None:
        return runtime.finish("uncertain", "cancel_not_confirmed")
    observation_error = runtime.observe(verified)
    if observation_error is not None:
        return runtime.finish("failed", observation_error)
    if verified.status == "canceled" and verified.cancellation_reason == "COULD_NOT_FILL":
        state.post_only_rejections += 1
        state.venue_cancels += 1
        return runtime.finish("failed", "post_only_rejected")
    state.cancels += 1
    state.requotes += 1
    runtime.record({"event": "cancel", "reason": decision.reason, "order_id": order.order_id})
    state.active = None
    state.active_submitted_ms = None
    state.last_filled = 0.0
    state.last_quote = 0.0
    if state.requotes > request.max_requotes:
        return runtime.finish("failed", "max_requotes_exhausted")
    return None


def _retry_order_observation(
    runtime: ExecutionRuntime,
    terminator: TerminationController,
    order_id: str,
    *,
    error: str | None = None,
    reason: str | None = None,
) -> TargetExecutionResult | None:
    state = runtime.state
    state.observation_errors += 1
    state.consecutive_observation_errors += 1
    event = "observation_error" if error is not None else "observation_unknown"
    runtime.record(
        {
            "event": event,
            "attempt": state.consecutive_observation_errors,
            "total": state.observation_errors,
            **({"error": error} if error is not None else {"reason": reason}),
        }
    )
    if state.consecutive_observation_errors >= runtime.request.max_observation_errors:
        return terminator.stop_after_observation_failure("order_observation_unavailable")
    delay = min(10_000, runtime.request.poll_interval_ms * (2 ** (state.consecutive_observation_errors - 1)))
    bounded_delay = min(delay, max(0, runtime.request.deadline_ms - (runtime.venue.now_ms - runtime.started_ms)))
    runtime.record_wait(
        "order_observation_retry",
        bounded_delay,
        force=True,
        order_id=order_id,
        attempt=state.consecutive_observation_errors,
        max_attempts=runtime.request.max_observation_errors,
    )
    runtime.venue.advance(bounded_delay)
    return None


def _submit_next_order(
    runtime: ExecutionRuntime,
    policy: MakerPolicy,
    terminator: TerminationController,
    remaining: float,
) -> TargetExecutionResult | None:
    venue, request, state = runtime.venue, runtime.request, runtime.state
    submission_wait_ms = getattr(venue, "submission_wait_ms", lambda: 0)()
    if submission_wait_ms > 0:
        runtime.record_wait("submission_slot", submission_wait_ms, force=True)
    venue.wait_for_submission_slot()
    if runtime.should_stop():
        return terminator.stop_for_request()
    if venue.now_ms - runtime.started_ms > request.deadline_ms:
        return terminator.finish_deadline()
    try:
        snapshot = runtime.read_snapshot()
    except ObservationUnavailableError as exc:
        return terminator.stop_after_observation_failure(exc.reason)
    urgency = min(1.0, (venue.now_ms - runtime.started_ms) / request.deadline_ms)
    decision = policy.decide(snapshot, request.side, remaining, urgency)
    if decision.action != "quote" or decision.price is None:
        return runtime.finish("failed", "policy_did_not_quote")
    if not _is_post_only_price(snapshot, request.side, decision.price):
        return runtime.finish("failed", "policy_would_take_liquidity")
    quantity = min(remaining, max(request.tolerance_quantity, remaining * policy.config.child_fraction))
    client_order_id = f"{request.client_prefix}-{state.submissions + 1:03d}"
    order = venue.submit_post_only(request.side, quantity, decision.price, client_order_id)
    if order.status == "not_submitted":
        state.preflight_skips += 1
        runtime.record(
            {
                "event": "preflight_skip",
                "reason": order.cancellation_reason or "local_price_would_take",
                "price": order.price,
            }
        )
        if state.preflight_skips > request.max_preflight_skips:
            return runtime.finish("failed", "max_preflight_skips_exhausted")
        if str(order.cancellation_reason or "").startswith("LOCAL_BOOK_UNAVAILABLE"):
            delay = min(2_000, 250 * (2 ** (state.preflight_skips - 1)))
        else:
            delay = min(request.poll_interval_ms, 250)
        runtime.record_wait(
            "submission_preflight_retry",
            delay,
            force=True,
            reason=order.cancellation_reason or "local_price_would_take",
        )
        venue.advance(delay)
        return None
    state.submissions += 1
    if order.status == "rejected":
        state.post_only_rejections += 1
        return runtime.finish("failed", "post_only_rejected")
    if not order.post_only:
        return runtime.finish("failed", "venue_did_not_accept_post_only")
    state.active = order
    state.active_submitted_ms = venue.now_ms
    state.last_filled = 0.0
    state.last_quote = 0.0
    runtime.record(
        {
            "event": "submit",
            "submitted_ms": venue.now_ms,
            "order_id": order.order_id,
            "price": order.price,
            "quantity": order.quantity,
            "decision": decision.reason,
        }
    )
    if order.status in {"new", "partially_filled", "unknown"}:
        runtime.record_wait(
            "maker_fill",
            request.poll_interval_ms,
            force=True,
            order_id=order.order_id,
            status=order.status,
            filled_quantity=order.filled_quantity,
            order_quantity=order.quantity,
            remaining_quantity=remaining,
        )
    venue.advance(request.poll_interval_ms)
    return None


def _is_post_only_price(snapshot: MarketSnapshot, side: Side, price: float) -> bool:
    return price < snapshot.ask if side == "buy" else price > snapshot.bid

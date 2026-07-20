from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Literal, Protocol, TypeVar

from weex_cli.adaptive_maker import MakerPolicy, MarketSnapshot, Side, WorkingQuote
from weex_cli.errors import ValidationError

OrderStatus = Literal["not_submitted", "new", "partially_filled", "filled", "canceled", "rejected", "unknown"]
ProgressSink = Callable[[Mapping[str, object]], None]
WAIT_HEARTBEAT_MS = 2_000
READ_OBSERVATION_ATTEMPTS = 3
_ObservationT = TypeVar("_ObservationT")


class ObservationUnavailableError(RuntimeError):
    """Raised when a bounded read-only observation cannot be completed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class VenueOrder:
    order_id: str
    client_order_id: str
    side: Side
    price: float
    quantity: float
    filled_quantity: float
    cumulative_quote: float
    status: OrderStatus
    post_only: bool
    maker: bool | None
    queue_ahead: float = 0.0
    cancellation_reason: str | None = None


class MakerVenue(Protocol):
    @property
    def now_ms(self) -> int: ...

    def snapshot(self) -> MarketSnapshot: ...

    def position_quantity(self) -> float: ...

    def wait_for_submission_slot(self) -> None: ...

    def submit_post_only(self, side: Side, quantity: float, price: float, client_order_id: str) -> VenueOrder: ...

    def fetch_order(self, order_id: str, client_order_id: str) -> VenueOrder: ...

    def cancel_order(self, order_id: str, client_order_id: str) -> VenueOrder: ...

    def advance(self, milliseconds: int) -> None: ...


@dataclass(frozen=True)
class TargetRequest:
    side: Side
    target_position: float
    deadline_ms: int = 30_000
    poll_interval_ms: int = 100
    max_requotes: int = 50
    max_preflight_skips: int = 10
    max_observation_errors: int = 12
    max_cancel_verification_attempts: int = 5
    tolerance_quantity: float = 1e-9
    client_prefix: str = "adaptive-maker"

    def __post_init__(self) -> None:
        if not math.isfinite(self.target_position) or self.deadline_ms <= 0 or self.poll_interval_ms <= 0:
            raise ValidationError("target request values are invalid")
        if (
            self.max_requotes < 0
            or self.max_preflight_skips < 0
            or self.max_observation_errors < 1
            or self.max_cancel_verification_attempts < 1
            or not math.isfinite(self.tolerance_quantity)
            or self.tolerance_quantity < 0
        ):
            raise ValidationError("target request requotes or tolerance are invalid")


@dataclass(frozen=True)
class TargetExecutionResult:
    status: str
    reason: str
    elapsed_ms: int
    start_position: float
    final_position: float
    target_position: float
    quote_volume: float
    fill_count: int
    submissions: int
    cancels: int
    venue_cancels: int
    preflight_skips: int
    observation_errors: int
    cancel_verification_attempts: int
    cancel_verification_errors: int
    requotes: int
    maker_only: bool
    post_only_rejections: int
    events: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["events"] = list(self.events)
        return payload


def execute_adaptive_maker_target(
    venue: MakerVenue,
    policy: MakerPolicy,
    request: TargetRequest,
    *,
    progress_sink: ProgressSink | None = None,
) -> TargetExecutionResult:
    started = venue.now_ms
    active: VenueOrder | None = None
    active_submitted_ms: int | None = None
    last_filled = 0.0
    last_quote = 0.0
    quote_volume = 0.0
    fill_count = 0
    submissions = 0
    cancels = 0
    venue_cancels = 0
    preflight_skips = 0
    observation_errors = 0
    consecutive_observation_errors = 0
    cancel_verification_attempts = 0
    cancel_verification_errors = 0
    requotes = 0
    post_only_rejections = 0
    maker_only = True
    events: list[dict[str, object]] = []
    last_wait_key: tuple[str, str] | None = None
    last_wait_emitted_ms: int | None = None
    last_position: float | None = None

    def record(event: dict[str, object]) -> None:
        events.append(event)
        if progress_sink is None:
            return
        try:
            progress_sink(event)
        except Exception:  # noqa: BLE001 - progress reporting must never alter execution
            return

    def record_wait(
        waiting_for: str,
        delay_ms: int | None,
        *,
        force: bool = False,
        order_id: str | None = None,
        **fields: object,
    ) -> None:
        nonlocal last_wait_emitted_ms, last_wait_key
        now = venue.now_ms
        key = (waiting_for, order_id or "")
        if (
            not force
            and key == last_wait_key
            and last_wait_emitted_ms is not None
            and now - last_wait_emitted_ms < WAIT_HEARTBEAT_MS
        ):
            return
        last_wait_key = key
        last_wait_emitted_ms = now
        record(
            {
                "event": "wait",
                "waiting_for": waiting_for,
                "elapsed_ms": max(0, now - started),
                "remaining_ms": max(0, request.deadline_ms - (now - started)),
                "next_check_ms": delay_ms,
                "order_id": order_id,
                **fields,
            }
        )

    def read_observation(
        kind: str,
        reader: Callable[[], _ObservationT],
        *,
        order_id: str | None = None,
    ) -> _ObservationT:
        nonlocal observation_errors
        attempts = min(READ_OBSERVATION_ATTEMPTS, request.max_observation_errors)
        for attempt in range(1, attempts + 1):
            try:
                value = reader()
            except Exception as exc:  # noqa: BLE001 - bounded retry is read-only
                observation_errors += 1
                record(
                    {
                        "event": f"{kind}_observation_error",
                        "error": type(exc).__name__,
                        "attempt": attempt,
                        "max_attempts": attempts,
                        "order_id": order_id,
                    }
                )
                if attempt >= attempts:
                    raise ObservationUnavailableError(f"{kind}_observation_unavailable") from exc
                delay = min(2_000, max(250, request.poll_interval_ms) * (2 ** (attempt - 1)))
                record_wait(
                    f"{kind}_observation_retry",
                    delay,
                    force=True,
                    order_id=order_id,
                    attempt=attempt + 1,
                    max_attempts=attempts,
                )
                venue.advance(delay)
                continue
            if attempt > 1:
                record({"event": f"{kind}_observation_recovered", "attempts": attempt, "order_id": order_id})
            return value
        raise AssertionError("unreachable")

    def read_position(*, order_id: str | None = None) -> float:
        nonlocal last_position
        last_position = read_observation("position", venue.position_quantity, order_id=order_id)
        return last_position

    def read_snapshot(*, order_id: str | None = None) -> MarketSnapshot:
        return read_observation("market", venue.snapshot, order_id=order_id)

    set_progress_sink = getattr(venue, "set_progress_sink", None)
    if callable(set_progress_sink):

        def venue_progress(event: Mapping[str, object]) -> None:
            detail = dict(event)
            if detail.pop("event", None) == "wait":
                delay_ms = int(detail.pop("next_check_ms", 0) or 0)
                waiting_for = str(detail.pop("waiting_for", "exchange_read"))
                record_wait(waiting_for, delay_ms, force=True, **detail)
                return
            record(detail)

        set_progress_sink(venue_progress)

    start_position = read_position()

    def observe(order: VenueOrder) -> str | None:
        nonlocal fill_count, last_filled, last_quote, maker_only, quote_volume
        delta_filled = max(0.0, order.filled_quantity - last_filled)
        delta_quote = max(0.0, order.cumulative_quote - last_quote)
        if delta_filled > request.tolerance_quantity:
            fill_count += 1
            quote_volume += delta_quote
            record(
                {
                    "event": "fill",
                    "order_id": order.order_id,
                    "quantity": delta_filled,
                    "quote": delta_quote,
                    "maker": order.maker,
                }
            )
            if order.maker is not True or not order.post_only:
                maker_only = False
                return "taker_fill_detected"
        last_filled = order.filled_quantity
        last_quote = order.cumulative_quote
        return None

    def finish(status: str, reason: str, *, final_position: float | None = None) -> TargetExecutionResult:
        resolved_position = last_position if final_position is None else final_position
        return TargetExecutionResult(
            status=status,
            reason=reason,
            elapsed_ms=venue.now_ms - started,
            start_position=start_position,
            final_position=start_position if resolved_position is None else resolved_position,
            target_position=request.target_position,
            quote_volume=quote_volume,
            fill_count=fill_count,
            submissions=submissions,
            cancels=cancels,
            venue_cancels=venue_cancels,
            preflight_skips=preflight_skips,
            observation_errors=observation_errors,
            cancel_verification_attempts=cancel_verification_attempts,
            cancel_verification_errors=cancel_verification_errors,
            requotes=requotes,
            maker_only=maker_only,
            post_only_rejections=post_only_rejections,
            events=tuple(events),
        )

    def cancel_and_verify(order: VenueOrder, *, capture_position: bool = True) -> VenueOrder | None:
        nonlocal cancel_verification_attempts, cancel_verification_errors
        position_before_cancel: float | None = None
        if capture_position:
            try:
                position_before_cancel = venue.position_quantity()
            except Exception:  # noqa: BLE001 - position proof is optional; do not delay the single cancel request
                position_before_cancel = None
        response: VenueOrder | None = None
        last_verified: VenueOrder | None = None
        record({"event": "cancel_started", "order_id": order.order_id})

        def reconcile_absent(verified: VenueOrder, attempt: int) -> VenueOrder | None:
            nonlocal cancel_verification_errors
            if (
                response is None
                or response.cancellation_reason != "OPEN_ORDER_ABSENT"
                or verified.status != "unknown"
                or verified.cancellation_reason
                not in {"OPEN_ORDER_ABSENT", "V3_CANCELED_REASON_UNKNOWN", "CANCELED_REASON_UNKNOWN"}
            ):
                return None
            try:
                if position_before_cancel is None:
                    return None
                position_after_cancel = read_position(order_id=order.order_id)
            except Exception as exc:  # noqa: BLE001 - leave the cancellation uncertain when position cannot be checked
                cancel_verification_errors += 1
                record(
                    {
                        "event": "cancel_position_verification_error",
                        "error": type(exc).__name__,
                        "order_id": order.order_id,
                    }
                )
                return None
            if abs(position_after_cancel - position_before_cancel) > request.tolerance_quantity:
                return None
            record(
                {
                    "event": "cancel_reconciled_absent",
                    "attempts": attempt,
                    "order_id": order.order_id,
                }
            )
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

        try:
            response = venue.cancel_order(order.order_id, order.client_order_id)
            record({"event": "cancel_response", "status": response.status, "order_id": order.order_id})
        except Exception as exc:  # noqa: BLE001 - the cancel may have reached WEEX; verify without resubmitting it
            cancel_verification_errors += 1
            record(
                {
                    "event": "cancel_request_error",
                    "error": type(exc).__name__,
                    "order_id": order.order_id,
                }
            )

        for attempt in range(1, request.max_cancel_verification_attempts + 1):
            cancel_verification_attempts += 1
            try:
                verified = venue.fetch_order(order.order_id, order.client_order_id)
            except Exception as exc:  # noqa: BLE001 - bounded read-only verification after one cancel request
                cancel_verification_errors += 1
                record(
                    {
                        "event": "cancel_verification_error",
                        "attempt": attempt,
                        "error": type(exc).__name__,
                        "order_id": order.order_id,
                    }
                )
            else:
                last_verified = verified
                record(
                    {
                        "event": "cancel_verification",
                        "attempt": attempt,
                        "status": verified.status,
                        "order_id": order.order_id,
                    }
                )
                if verified.status in {"filled", "canceled"}:
                    return verified
                reconciled = reconcile_absent(verified, attempt)
                if reconciled is not None:
                    return reconciled
            if attempt < request.max_cancel_verification_attempts:
                base_delay = max(250, request.poll_interval_ms)
                delay = min(2_000, base_delay * (2 ** (attempt - 1)))
                record_wait(
                    "cancel_confirmation",
                    delay,
                    force=True,
                    order_id=order.order_id,
                    attempt=attempt + 1,
                    max_attempts=request.max_cancel_verification_attempts,
                )
                venue.advance(delay)
        if last_verified is not None:
            return reconcile_absent(last_verified, request.max_cancel_verification_attempts)
        return None

    def stop_after_observation_failure(reason: str) -> TargetExecutionResult:
        """Contain a known live order before returning an observation failure."""
        nonlocal active, cancels, venue_cancels, post_only_rejections
        if active is None:
            return finish("uncertain", reason)
        record({"event": "observation_cleanup_started", "reason": reason, "order_id": active.order_id})
        verified = cancel_and_verify(active, capture_position=reason != "position_observation_unavailable")
        if verified is None:
            record({"event": "observation_cleanup_not_confirmed", "reason": reason, "order_id": active.order_id})
            return finish("uncertain", "cancel_not_confirmed")
        observation_error = observe(verified)
        if observation_error is not None:
            return finish("failed", observation_error)
        if verified.status == "canceled" and verified.cancellation_reason == "COULD_NOT_FILL":
            post_only_rejections += 1
            venue_cancels += 1
            return finish("failed", "post_only_rejected")
        if verified.status not in {"filled", "canceled"}:
            return finish("uncertain", "cancel_not_confirmed")
        if verified.status == "canceled":
            cancels += 1
        active = None
        record({"event": "observation_cleanup_confirmed", "reason": reason, "order_id": verified.order_id})
        try:
            final_position = read_position(order_id=verified.order_id)
        except ObservationUnavailableError:
            final_position = None
        return finish("uncertain", reason, final_position=final_position)

    def finish_deadline() -> TargetExecutionResult:
        """Close the timeout boundary before allowing a caller to recover exposure."""
        nonlocal active, cancels, venue_cancels, post_only_rejections
        cleanup = getattr(venue, "cancel_all_and_verify", None)
        if callable(cleanup):
            record({"event": "timeout_cleanup_started"})
            try:
                verified_empty = bool(cleanup())
            except Exception as exc:  # noqa: BLE001 - cleanup uncertainty is terminal
                record(
                    {
                        "event": "timeout_cleanup_error",
                        "error": type(exc).__name__,
                    }
                )
                return finish("uncertain", "deadline_cleanup_not_confirmed")
            if not verified_empty:
                record({"event": "timeout_cleanup_not_confirmed"})
                return finish("uncertain", "deadline_cleanup_not_confirmed")
            record({"event": "timeout_cleanup_confirmed"})

            # The batch cleanup proves absence of live orders.  Re-read the
            # known active order for fill accounting, but never issue a second
            # per-order cancellation after the batch request.
            if active is not None:
                try:
                    verified = venue.fetch_order(active.order_id, active.client_order_id)
                except Exception as exc:  # noqa: BLE001 - read-only reconciliation
                    record(
                        {
                            "event": "timeout_order_verification_error",
                            "error": type(exc).__name__,
                            "order_id": active.order_id,
                        }
                    )
                    return finish("uncertain", "deadline_order_not_confirmed")
                observation_error = observe(verified)
                if observation_error is not None:
                    return finish("failed", observation_error)
                if verified.status not in {"filled", "canceled"}:
                    record(
                        {
                            "event": "timeout_order_not_confirmed",
                            "order_id": active.order_id,
                            "status": verified.status,
                        }
                    )
                    return finish("uncertain", "deadline_order_not_confirmed")
                active = None
        elif active is not None:
            # Keep simulator and custom venues on the original bounded
            # per-order path when they do not expose symbol-wide cleanup.
            verified = cancel_and_verify(active)
            if verified is None:
                return finish("uncertain", "deadline_cancel_not_confirmed")
            observation_error = observe(verified)
            if observation_error is not None:
                return finish("failed", observation_error)
            if verified.status == "canceled" and verified.cancellation_reason == "COULD_NOT_FILL":
                post_only_rejections += 1
                venue_cancels += 1
                return finish("failed", "post_only_rejected")
            cancels += 1
            active = None

        try:
            final_position = read_position()
        except ObservationUnavailableError:
            return finish("uncertain", "position_observation_unavailable")
        if abs(request.target_position - final_position) <= request.tolerance_quantity:
            return finish("completed", "target_reached", final_position=final_position)
        return finish("failed", "deadline_exceeded")

    while venue.now_ms - started <= request.deadline_ms:
        try:
            current = read_position(order_id=active.order_id if active is not None else None)
        except ObservationUnavailableError as exc:
            return stop_after_observation_failure(exc.reason)
        if request.side == "buy" and current > request.target_position + request.tolerance_quantity:
            return finish("failed", "target_overfilled")
        if request.side == "sell" and current < request.target_position - request.tolerance_quantity:
            return finish("failed", "target_overfilled")
        remaining = abs(request.target_position - current)

        if active is not None:
            try:
                order = venue.fetch_order(active.order_id, active.client_order_id)
            except Exception as exc:  # noqa: BLE001 - retry only this read-only observation
                observation_errors += 1
                consecutive_observation_errors += 1
                record(
                    {
                        "event": "observation_error",
                        "error": type(exc).__name__,
                        "attempt": consecutive_observation_errors,
                        "total": observation_errors,
                    }
                )
                if consecutive_observation_errors >= request.max_observation_errors:
                    return stop_after_observation_failure("order_observation_unavailable")
                delay = min(10_000, request.poll_interval_ms * (2 ** (consecutive_observation_errors - 1)))
                bounded_delay = min(delay, max(0, request.deadline_ms - (venue.now_ms - started)))
                record_wait(
                    "order_observation_retry",
                    bounded_delay,
                    force=True,
                    order_id=active.order_id,
                    attempt=consecutive_observation_errors,
                    max_attempts=request.max_observation_errors,
                )
                venue.advance(bounded_delay)
                continue
            active = order

            if order.status == "unknown":
                observation_errors += 1
                consecutive_observation_errors += 1
                record(
                    {
                        "event": "observation_unknown",
                        "attempt": consecutive_observation_errors,
                        "total": observation_errors,
                        "reason": order.cancellation_reason,
                    }
                )
                if consecutive_observation_errors >= request.max_observation_errors:
                    return stop_after_observation_failure("order_observation_unavailable")
                delay = min(10_000, request.poll_interval_ms * (2 ** (consecutive_observation_errors - 1)))
                bounded_delay = min(delay, max(0, request.deadline_ms - (venue.now_ms - started)))
                record_wait(
                    "order_observation_retry",
                    bounded_delay,
                    force=True,
                    order_id=active.order_id,
                    attempt=consecutive_observation_errors,
                    max_attempts=request.max_observation_errors,
                )
                venue.advance(bounded_delay)
                continue
            consecutive_observation_errors = 0
            observation_error = observe(order)
            if observation_error is not None:
                return finish("failed", observation_error)
            if order.status == "rejected":
                if order.post_only:
                    post_only_rejections += 1
                return finish("failed", "post_only_rejected")
            if order.status == "canceled" and order.cancellation_reason == "COULD_NOT_FILL":
                post_only_rejections += 1
                venue_cancels += 1
                record(
                    {
                        "event": "post_only_rejection",
                        "order_id": order.order_id,
                        "reason": order.cancellation_reason,
                    }
                )
                return finish("failed", "post_only_rejected")
            if order.status in {"filled", "canceled"}:
                if order.status == "canceled":
                    venue_cancels += 1
                record({"event": "order_terminal", "status": order.status, "order_id": order.order_id})
                active = None
                last_filled = 0.0
                last_quote = 0.0
                continue

            try:
                snapshot = read_snapshot(order_id=order.order_id)
            except ObservationUnavailableError as exc:
                return stop_after_observation_failure(exc.reason)
            elapsed = venue.now_ms - started
            urgency = min(1.0, elapsed / request.deadline_ms)
            working = WorkingQuote(
                side=order.side,
                price=order.price,
                submitted_ms=active_submitted_ms if active_submitted_ms is not None else venue.now_ms,
                queue_ahead=order.queue_ahead,
                remaining_quantity=max(0.0, order.quantity - order.filled_quantity),
            )
            decision = policy.decide(
                snapshot,
                request.side,
                max(remaining, request.tolerance_quantity),
                urgency,
                working,
            )
            if decision.action == "cancel":
                verified = cancel_and_verify(order)
                if verified is None:
                    return finish("uncertain", "cancel_not_confirmed")
                observation_error = observe(verified)
                if observation_error is not None:
                    return finish("failed", observation_error)
                if verified.status == "canceled" and verified.cancellation_reason == "COULD_NOT_FILL":
                    post_only_rejections += 1
                    venue_cancels += 1
                    return finish("failed", "post_only_rejected")
                cancels += 1
                requotes += 1
                record({"event": "cancel", "reason": decision.reason, "order_id": order.order_id})
                active = None
                active_submitted_ms = None
                last_filled = 0.0
                last_quote = 0.0
                if requotes > request.max_requotes:
                    return finish("failed", "max_requotes_exhausted")
                continue

            record_wait(
                "maker_fill",
                request.poll_interval_ms,
                order_id=order.order_id,
                status=order.status,
                filled_quantity=order.filled_quantity,
                order_quantity=order.quantity,
                remaining_quantity=remaining,
            )
            venue.advance(request.poll_interval_ms)
            continue

        if remaining <= request.tolerance_quantity:
            return finish("completed", "target_reached")

        # The Demo venue may need to wait 10.1 seconds between submissions.
        # Wait first so the quote is derived from the freshest available book.
        submission_wait_ms = getattr(venue, "submission_wait_ms", lambda: 0)()
        if submission_wait_ms > 0:
            record_wait("submission_slot", submission_wait_ms, force=True)
        venue.wait_for_submission_slot()
        if venue.now_ms - started > request.deadline_ms:
            return finish_deadline()
        try:
            snapshot = read_snapshot()
        except ObservationUnavailableError as exc:
            return stop_after_observation_failure(exc.reason)
        elapsed = venue.now_ms - started
        urgency = min(1.0, elapsed / request.deadline_ms)
        decision = policy.decide(snapshot, request.side, remaining, urgency)
        if decision.action != "quote" or decision.price is None:
            return finish("failed", "policy_did_not_quote")
        if not _is_post_only_price(snapshot, request.side, decision.price):
            return finish("failed", "policy_would_take_liquidity")

        quantity = min(remaining, max(request.tolerance_quantity, remaining * policy.config.child_fraction))
        client_order_id = f"{request.client_prefix}-{submissions + 1:03d}"
        order = venue.submit_post_only(request.side, quantity, decision.price, client_order_id)
        if order.status == "not_submitted":
            preflight_skips += 1
            record(
                {
                    "event": "preflight_skip",
                    "reason": order.cancellation_reason or "local_price_would_take",
                    "price": order.price,
                }
            )
            if preflight_skips > request.max_preflight_skips:
                return finish("failed", "max_preflight_skips_exhausted")
            if str(order.cancellation_reason or "").startswith("LOCAL_BOOK_UNAVAILABLE"):
                delay = min(2_000, 250 * (2 ** (preflight_skips - 1)))
            else:
                delay = min(request.poll_interval_ms, 250)
            record_wait(
                "submission_preflight_retry",
                delay,
                force=True,
                reason=order.cancellation_reason or "local_price_would_take",
            )
            venue.advance(delay)
            continue
        submissions += 1
        if order.status == "rejected":
            post_only_rejections += 1
            return finish("failed", "post_only_rejected")
        if not order.post_only:
            return finish("failed", "venue_did_not_accept_post_only")
        active = order
        active_submitted_ms = venue.now_ms
        last_filled = 0.0
        last_quote = 0.0
        record(
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
            record_wait(
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

    return finish_deadline()


def _is_post_only_price(snapshot: MarketSnapshot, side: Side, price: float) -> bool:
    if side == "buy":
        return price < snapshot.ask
    return price > snapshot.bid

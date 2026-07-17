from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

from weex_cli.adaptive_maker import MakerPolicy, MarketSnapshot, Side, WorkingQuote
from weex_cli.errors import ValidationError

OrderStatus = Literal["not_submitted", "new", "partially_filled", "filled", "canceled", "rejected", "unknown"]


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
        if (
            not math.isfinite(self.target_position)
            or self.target_position < 0
            or self.deadline_ms <= 0
            or self.poll_interval_ms <= 0
        ):
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
) -> TargetExecutionResult:
    started = venue.now_ms
    start_position = venue.position_quantity()
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

    def observe(order: VenueOrder) -> str | None:
        nonlocal fill_count, last_filled, last_quote, maker_only, quote_volume
        delta_filled = max(0.0, order.filled_quantity - last_filled)
        delta_quote = max(0.0, order.cumulative_quote - last_quote)
        if delta_filled > request.tolerance_quantity:
            fill_count += 1
            quote_volume += delta_quote
            events.append(
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

    def finish(status: str, reason: str) -> TargetExecutionResult:
        return TargetExecutionResult(
            status=status,
            reason=reason,
            elapsed_ms=venue.now_ms - started,
            start_position=start_position,
            final_position=venue.position_quantity(),
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

    def cancel_and_verify(order: VenueOrder) -> VenueOrder | None:
        nonlocal cancel_verification_attempts, cancel_verification_errors
        position_before_cancel = venue.position_quantity()
        response: VenueOrder | None = None
        last_verified: VenueOrder | None = None

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
                position_after_cancel = venue.position_quantity()
            except Exception as exc:  # noqa: BLE001 - leave the cancellation uncertain when position cannot be checked
                cancel_verification_errors += 1
                events.append(
                    {
                        "event": "cancel_position_verification_error",
                        "error": type(exc).__name__,
                        "order_id": order.order_id,
                    }
                )
                return None
            if abs(position_after_cancel - position_before_cancel) > request.tolerance_quantity:
                return None
            events.append(
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
            events.append({"event": "cancel_response", "status": response.status, "order_id": order.order_id})
        except Exception as exc:  # noqa: BLE001 - the cancel may have reached WEEX; verify without resubmitting it
            cancel_verification_errors += 1
            events.append(
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
                events.append(
                    {
                        "event": "cancel_verification_error",
                        "attempt": attempt,
                        "error": type(exc).__name__,
                        "order_id": order.order_id,
                    }
                )
            else:
                last_verified = verified
                events.append(
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
                venue.advance(min(2_000, base_delay * (2 ** (attempt - 1))))
        if last_verified is not None:
            return reconcile_absent(last_verified, request.max_cancel_verification_attempts)
        return None

    while venue.now_ms - started <= request.deadline_ms:
        current = venue.position_quantity()
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
                events.append(
                    {
                        "event": "observation_error",
                        "error": type(exc).__name__,
                        "attempt": consecutive_observation_errors,
                        "total": observation_errors,
                    }
                )
                if consecutive_observation_errors >= request.max_observation_errors:
                    return finish("uncertain", "order_observation_unavailable")
                delay = min(10_000, request.poll_interval_ms * (2 ** (consecutive_observation_errors - 1)))
                venue.advance(min(delay, max(0, request.deadline_ms - (venue.now_ms - started))))
                continue
            active = order

            if order.status == "unknown":
                observation_errors += 1
                consecutive_observation_errors += 1
                events.append(
                    {
                        "event": "observation_unknown",
                        "attempt": consecutive_observation_errors,
                        "total": observation_errors,
                        "reason": order.cancellation_reason,
                    }
                )
                if consecutive_observation_errors >= request.max_observation_errors:
                    return finish("uncertain", "order_observation_unavailable")
                delay = min(10_000, request.poll_interval_ms * (2 ** (consecutive_observation_errors - 1)))
                venue.advance(min(delay, max(0, request.deadline_ms - (venue.now_ms - started))))
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
                events.append(
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
                events.append({"event": "order_terminal", "status": order.status, "order_id": order.order_id})
                active = None
                last_filled = 0.0
                last_quote = 0.0
                continue

            snapshot = venue.snapshot()
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
                events.append({"event": "cancel", "reason": decision.reason, "order_id": order.order_id})
                active = None
                active_submitted_ms = None
                last_filled = 0.0
                last_quote = 0.0
                if requotes > request.max_requotes:
                    return finish("failed", "max_requotes_exhausted")
                continue

            venue.advance(request.poll_interval_ms)
            continue

        if remaining <= request.tolerance_quantity:
            return finish("completed", "target_reached")

        # The Demo venue may need to wait 10.1 seconds between submissions.
        # Wait first so the quote is derived from the freshest available book.
        venue.wait_for_submission_slot()
        if venue.now_ms - started > request.deadline_ms:
            return finish("failed", "deadline_exceeded")
        snapshot = venue.snapshot()
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
            events.append(
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
        events.append(
            {
                "event": "submit",
                "submitted_ms": venue.now_ms,
                "order_id": order.order_id,
                "price": order.price,
                "quantity": order.quantity,
                "decision": decision.reason,
            }
        )
        venue.advance(request.poll_interval_ms)

    if active is None and abs(request.target_position - venue.position_quantity()) <= request.tolerance_quantity:
        return finish("completed", "target_reached")
    if active is not None:
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
        if abs(request.target_position - venue.position_quantity()) <= request.tolerance_quantity:
            return finish("completed", "target_reached")
    return finish("failed", "deadline_exceeded")


def _is_post_only_price(snapshot: MarketSnapshot, side: Side, price: float) -> bool:
    if side == "buy":
        return price < snapshot.ask
    return price > snapshot.bid

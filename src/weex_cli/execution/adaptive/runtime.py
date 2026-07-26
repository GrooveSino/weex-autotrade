from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TypeVar

from weex_cli.execution.adaptive_maker import MarketSnapshot

from .contracts import (
    READ_OBSERVATION_ATTEMPTS,
    WAIT_HEARTBEAT_MS,
    MakerVenue,
    ObservationUnavailableError,
    ProgressSink,
    TargetExecutionResult,
    TargetRequest,
    VenueOrder,
)

_ObservationT = TypeVar("_ObservationT")


@dataclass
class ExecutionState:
    start_position: float = 0.0
    active: VenueOrder | None = None
    active_submitted_ms: int | None = None
    last_filled: float = 0.0
    last_quote: float = 0.0
    quote_volume: float = 0.0
    fill_count: int = 0
    submissions: int = 0
    cancels: int = 0
    venue_cancels: int = 0
    preflight_skips: int = 0
    observation_errors: int = 0
    consecutive_observation_errors: int = 0
    cancel_verification_attempts: int = 0
    cancel_verification_errors: int = 0
    requotes: int = 0
    post_only_rejections: int = 0
    maker_only: bool = True
    last_wait_key: tuple[str, str] | None = None
    last_wait_emitted_ms: int | None = None
    last_position: float | None = None
    events: list[dict[str, object]] = field(default_factory=list)


class ExecutionRuntime:
    def __init__(
        self,
        venue: MakerVenue,
        request: TargetRequest,
        progress_sink: ProgressSink | None,
        stop_requested: Callable[[], bool] | None,
    ) -> None:
        self.venue = venue
        self.request = request
        self.progress_sink = progress_sink
        self.should_stop = stop_requested or (lambda: False)
        self.started_ms = venue.now_ms
        self.state = ExecutionState()

    def record(self, event: dict[str, object]) -> None:
        self.state.events.append(event)
        if self.progress_sink is None:
            return
        try:
            self.progress_sink(event)
        except Exception:  # noqa: BLE001 - progress reporting must never alter execution
            return

    def record_wait(
        self,
        waiting_for: str,
        delay_ms: int | None,
        *,
        force: bool = False,
        order_id: str | None = None,
        **fields: object,
    ) -> None:
        now = self.venue.now_ms
        key = (waiting_for, order_id or "")
        state = self.state
        if (
            not force
            and key == state.last_wait_key
            and state.last_wait_emitted_ms is not None
            and now - state.last_wait_emitted_ms < WAIT_HEARTBEAT_MS
        ):
            return
        state.last_wait_key = key
        state.last_wait_emitted_ms = now
        self.record(
            {
                "event": "wait",
                "waiting_for": waiting_for,
                "elapsed_ms": max(0, now - self.started_ms),
                "remaining_ms": max(0, self.request.deadline_ms - (now - self.started_ms)),
                "next_check_ms": delay_ms,
                "order_id": order_id,
                **fields,
            }
        )

    def read_observation(
        self,
        kind: str,
        reader: Callable[[], _ObservationT],
        *,
        order_id: str | None = None,
    ) -> _ObservationT:
        attempts = min(READ_OBSERVATION_ATTEMPTS, self.request.max_observation_errors)
        for attempt in range(1, attempts + 1):
            try:
                value = reader()
            except Exception as exc:  # noqa: BLE001 - bounded retry is read-only
                self.state.observation_errors += 1
                self.record(
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
                delay = min(2_000, max(250, self.request.poll_interval_ms) * (2 ** (attempt - 1)))
                self.record_wait(
                    f"{kind}_observation_retry",
                    delay,
                    force=True,
                    order_id=order_id,
                    attempt=attempt + 1,
                    max_attempts=attempts,
                )
                self.venue.advance(delay)
            else:
                if attempt > 1:
                    self.record({"event": f"{kind}_observation_recovered", "attempts": attempt, "order_id": order_id})
                return value
        raise AssertionError("unreachable")

    def read_position(self, *, order_id: str | None = None) -> float:
        position = self.read_observation("position", self.venue.position_quantity, order_id=order_id)
        self.state.last_position = position
        return position

    def read_snapshot(self, *, order_id: str | None = None) -> MarketSnapshot:
        return self.read_observation("market", self.venue.snapshot, order_id=order_id)

    def bind_venue_progress(self) -> None:
        set_progress_sink = getattr(self.venue, "set_progress_sink", None)
        if not callable(set_progress_sink):
            return

        def venue_progress(event: Mapping[str, object]) -> None:
            detail = dict(event)
            if detail.pop("event", None) == "wait":
                delay_ms = int(detail.pop("next_check_ms", 0) or 0)
                self.record_wait(str(detail.pop("waiting_for", "exchange_read")), delay_ms, force=True, **detail)
                return
            self.record(detail)

        set_progress_sink(venue_progress)

    def observe(self, order: VenueOrder) -> str | None:
        state = self.state
        delta_filled = max(0.0, order.filled_quantity - state.last_filled)
        delta_quote = max(0.0, order.cumulative_quote - state.last_quote)
        if delta_filled > self.request.tolerance_quantity:
            state.fill_count += 1
            state.quote_volume += delta_quote
            self.record(
                {
                    "event": "fill",
                    "order_id": order.order_id,
                    "quantity": delta_filled,
                    "quote": delta_quote,
                    "maker": order.maker,
                }
            )
            if order.maker is not True or not order.post_only:
                state.maker_only = False
                return "taker_fill_detected"
        state.last_filled = order.filled_quantity
        state.last_quote = order.cumulative_quote
        return None

    def finish(self, status: str, reason: str, *, final_position: float | None = None) -> TargetExecutionResult:
        state = self.state
        resolved_position = state.last_position if final_position is None else final_position
        return TargetExecutionResult(
            status=status,
            reason=reason,
            elapsed_ms=self.venue.now_ms - self.started_ms,
            start_position=state.start_position,
            final_position=state.start_position if resolved_position is None else resolved_position,
            target_position=self.request.target_position,
            quote_volume=state.quote_volume,
            fill_count=state.fill_count,
            submissions=state.submissions,
            cancels=state.cancels,
            venue_cancels=state.venue_cancels,
            preflight_skips=state.preflight_skips,
            observation_errors=state.observation_errors,
            cancel_verification_attempts=state.cancel_verification_attempts,
            cancel_verification_errors=state.cancel_verification_errors,
            requotes=state.requotes,
            maker_only=state.maker_only,
            post_only_rejections=state.post_only_rejections,
            events=tuple(state.events),
        )

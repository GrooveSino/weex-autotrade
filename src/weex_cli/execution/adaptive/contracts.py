from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

from weex_cli.core.errors import ValidationError
from weex_cli.execution.adaptive_maker import MarketSnapshot, Side

OrderStatus = Literal["not_submitted", "new", "partially_filled", "filled", "canceled", "rejected", "unknown"]
ProgressSink = Callable[[Mapping[str, object]], None]
WAIT_HEARTBEAT_MS = 2_000
READ_OBSERVATION_ATTEMPTS = 3


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

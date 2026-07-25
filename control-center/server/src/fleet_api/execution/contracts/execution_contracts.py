from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from fleet_api.runtime.telemetry import AccountTelemetryContext
from fleet_api.volume.core.volume_history import NormalizedTradeFill

_REASON_CODE = re.compile(r"[a-z0-9_.:-]{1,80}")


class ExecutionStateError(RuntimeError):
    pass


class AllocationUnavailable(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        if _REASON_CODE.fullmatch(reason_code) is None:
            raise ValueError("allocation unavailable reason must be a sanitized reason code")
        self.reason_code = reason_code
        super().__init__(reason_code)


class PairDirection(StrEnum):
    LONG = "long"
    SHORT = "short"


class PairLegAction(StrEnum):
    OPEN = "open"
    CLOSE = "close"


class CycleExecutionStatus(StrEnum):
    PLANNED = "planned"
    OPENED = "opened"
    COMPLETED = "completed"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


_TERMINAL_STATUSES = {
    CycleExecutionStatus.COMPLETED,
    CycleExecutionStatus.REJECTED,
    CycleExecutionStatus.UNCERTAIN,
}


@dataclass(frozen=True, slots=True)
class PairAllocation:
    btc_weight: Decimal
    eth_weight: Decimal
    version: str

    def __post_init__(self) -> None:
        if not self.btc_weight.is_finite() or not self.eth_weight.is_finite():
            raise ValueError("pair allocation weights must be finite")
        if self.btc_weight <= 0 or self.eth_weight <= 0:
            raise ValueError("pair allocation weights must be positive")
        if self.btc_weight + self.eth_weight != Decimal(1):
            raise ValueError("pair allocation weights must sum to 1")
        if not self.version.strip():
            raise ValueError("pair allocation version cannot be empty")
        if len(self.version) > 80:
            raise ValueError("pair allocation version is too long")


@dataclass(frozen=True, slots=True)
class PairCyclePlan:
    cycle_id: str
    sequence: int
    total_quote: Decimal
    btc_long_quote: Decimal
    eth_short_quote: Decimal
    allocation_version: str
    turnover_quote: Decimal | None = None
    position_hold_seconds: int = 0
    round_interval_seconds: int = 0
    sizing_mode: str = "legacy_fixed"
    strategy_id: str = "legacy"

    def __post_init__(self) -> None:
        if not self.cycle_id.strip():
            raise ValueError("cycle id cannot be empty")
        if self.sequence < 1:
            raise ValueError("cycle sequence must be at least 1")
        if self.total_quote <= 0:
            raise ValueError("cycle total quote must be positive")
        if self.btc_long_quote <= 0 or self.eth_short_quote <= 0:
            raise ValueError("cycle leg quote amounts must be positive")
        if self.btc_long_quote + self.eth_short_quote != self.total_quote:
            raise ValueError("cycle leg quote amounts must equal total quote")
        turnover = self.turnover_quote if self.turnover_quote is not None else self.total_quote * 2
        if turnover != self.total_quote * 2:
            raise ValueError("cycle turnover quote must equal opening total times two")
        object.__setattr__(self, "turnover_quote", turnover)
        if self.position_hold_seconds < 0 or self.round_interval_seconds < 0:
            raise ValueError("cycle delays cannot be negative")
        if self.sizing_mode not in {"range_random", "residual_finish", "legacy_fixed"}:
            raise ValueError("unsupported cycle sizing mode")
        if not self.strategy_id.strip():
            raise ValueError("strategy id cannot be empty")


@dataclass(frozen=True, slots=True)
class PairExecutionLeg:
    symbol: str
    direction: PairDirection
    action: PairLegAction
    fill: NormalizedTradeFill


@dataclass(frozen=True, slots=True)
class PairExecutionOutcome:
    status: CycleExecutionStatus
    reason: str
    legs: tuple[PairExecutionLeg, ...] = ()

    def __post_init__(self) -> None:
        if self.status is CycleExecutionStatus.PLANNED:
            raise ValueError("adapter outcome cannot remain planned")
        if not self.reason.strip():
            raise ValueError("execution outcome reason cannot be empty")
        if _REASON_CODE.fullmatch(self.reason) is None:
            raise ValueError("execution outcome reason must be a sanitized reason code")
        if self.status in {CycleExecutionStatus.OPENED, CycleExecutionStatus.COMPLETED} and len(self.legs) != 2:
            raise ValueError("successful pair phase must contain exactly two legs")
        if self.status not in {CycleExecutionStatus.OPENED, CycleExecutionStatus.COMPLETED} and self.legs:
            raise ValueError("unsuccessful execution cannot claim fills")


@dataclass(frozen=True, slots=True)
class PositionCloseOutcome:
    status: CycleExecutionStatus
    reason: str
    legs: tuple[PairExecutionLeg, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {
            CycleExecutionStatus.COMPLETED,
            CycleExecutionStatus.REJECTED,
            CycleExecutionStatus.UNCERTAIN,
        }:
            raise ValueError("position close outcome must be terminal")
        if not self.reason.strip() or _REASON_CODE.fullmatch(self.reason) is None:
            raise ValueError("position close reason must be a sanitized reason code")
        if self.status is CycleExecutionStatus.COMPLETED and not 1 <= len(self.legs) <= 2:
            raise ValueError("successful position close must contain one or two legs")
        if self.status is not CycleExecutionStatus.COMPLETED and self.legs:
            raise ValueError("unsuccessful position close cannot claim fills")


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    instance_id: str
    plan: PairCyclePlan
    status: CycleExecutionStatus
    reason: str
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class BeginCycleResult:
    record: ExecutionRecord
    created: bool


@dataclass(frozen=True, slots=True)
class CycleExecutionResult:
    record: ExecutionRecord
    submitted: bool
    fills_inserted: int


@dataclass(frozen=True, slots=True)
class PositionCloseExecutionResult:
    outcome: PositionCloseOutcome
    record: ExecutionRecord | None
    submitted: bool
    fills_inserted: int

    @property
    def closed_quote(self) -> Decimal:
        return sum((leg.fill.quote_volume for leg in self.outcome.legs), start=Decimal(0))


@dataclass(frozen=True, slots=True)
class CancelOrdersOutcome:
    verified: bool
    canceled_count: int
    reason: str

    def __post_init__(self) -> None:
        if self.canceled_count < 0:
            raise ValueError("canceled order count cannot be negative")
        _require_reason_code(self.reason)


class PairAllocationProvider(Protocol):
    async def get(self, context: AccountTelemetryContext) -> PairAllocation: ...

    async def aclose(self) -> None: ...


class PairedExecutionAdapter(Protocol):
    async def open_once(
        self,
        context: AccountTelemetryContext,
        plan: PairCyclePlan,
    ) -> PairExecutionOutcome: ...

    async def close_once(
        self,
        context: AccountTelemetryContext,
        plan: PairCyclePlan,
    ) -> PairExecutionOutcome: ...

    async def close_positions_once(
        self,
        context: AccountTelemetryContext,
        operation_id: str,
    ) -> PositionCloseOutcome: ...

    async def cancel_active_orders(self, context: AccountTelemetryContext) -> CancelOrdersOutcome: ...

    async def aclose(self) -> None: ...


class PairedExecutionAdapterFactory(Protocol):
    def create(self, instance_id: str) -> PairedExecutionAdapter: ...


class ExecutionJournal(Protocol):
    def begin(self, instance_id: str, plan: PairCyclePlan) -> BeginCycleResult: ...

    def finish(
        self,
        cycle_id: str,
        status: CycleExecutionStatus,
        reason: str,
    ) -> ExecutionRecord: ...

    def find(self, instance_id: str, sequence: int) -> ExecutionRecord | None: ...

    def list_recent(self, instance_id: str, limit: int) -> list[ExecutionRecord]: ...

    def recover_incomplete(self) -> int: ...

    def remove(self, instance_id: str) -> None: ...

    def close(self) -> None: ...


def _require_reason_code(reason: str) -> None:
    if _REASON_CODE.fullmatch(reason) is None:
        raise ExecutionStateError("execution reason must be a sanitized reason code")

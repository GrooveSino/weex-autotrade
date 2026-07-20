from __future__ import annotations

import asyncio
import random
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Protocol
from uuid import uuid4

from .models import ExposureSnapshot, InstanceStatus, StrategyStage
from .strategy import plan_strategy_cycle, random_seconds, target_progress_quote
from .telemetry import AccountTelemetryContext
from .volume_history import NormalizedTradeFill, TradeVolumeLedger

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


class MockPairedExecutionAdapter:
    """Produces deterministic simulated fills and never opens a network connection."""

    async def open_once(
        self,
        context: AccountTelemetryContext,
        plan: PairCyclePlan,
    ) -> PairExecutionOutcome:
        executed_at_ms = time.time_ns() // 1_000_000
        return PairExecutionOutcome(
            status=CycleExecutionStatus.OPENED,
            reason="mock_pair_opened",
            legs=(
                PairExecutionLeg(
                    symbol="BTCUSDT",
                    direction=PairDirection.LONG,
                    action=PairLegAction.OPEN,
                    fill=NormalizedTradeFill(
                        identity=f"{plan.cycle_id}:btc-long-open",
                        executed_at_ms=executed_at_ms,
                        quote_volume=plan.btc_long_quote,
                        symbol="BTCUSDT",
                        position_action="open",
                        maker=True,
                        source="mock_execution",
                    ),
                ),
                PairExecutionLeg(
                    symbol="ETHUSDT",
                    direction=PairDirection.SHORT,
                    action=PairLegAction.OPEN,
                    fill=NormalizedTradeFill(
                        identity=f"{plan.cycle_id}:eth-short-open",
                        executed_at_ms=executed_at_ms,
                        quote_volume=plan.eth_short_quote,
                        symbol="ETHUSDT",
                        position_action="open",
                        maker=True,
                        source="mock_execution",
                    ),
                ),
            ),
        )

    async def close_once(
        self,
        context: AccountTelemetryContext,
        plan: PairCyclePlan,
    ) -> PairExecutionOutcome:
        del context
        executed_at_ms = time.time_ns() // 1_000_000
        return PairExecutionOutcome(
            status=CycleExecutionStatus.COMPLETED,
            reason="mock_pair_closed",
            legs=(
                PairExecutionLeg(
                    symbol="BTCUSDT",
                    direction=PairDirection.LONG,
                    action=PairLegAction.CLOSE,
                    fill=NormalizedTradeFill(
                        identity=f"{plan.cycle_id}:btc-long-close",
                        executed_at_ms=executed_at_ms,
                        quote_volume=plan.btc_long_quote,
                        symbol="BTCUSDT",
                        position_action="close",
                        maker=True,
                        source="mock_execution",
                    ),
                ),
                PairExecutionLeg(
                    symbol="ETHUSDT",
                    direction=PairDirection.SHORT,
                    action=PairLegAction.CLOSE,
                    fill=NormalizedTradeFill(
                        identity=f"{plan.cycle_id}:eth-short-close",
                        executed_at_ms=executed_at_ms,
                        quote_volume=plan.eth_short_quote,
                        symbol="ETHUSDT",
                        position_action="close",
                        maker=True,
                        source="mock_execution",
                    ),
                ),
            ),
        )

    async def close_positions_once(
        self,
        context: AccountTelemetryContext,
        operation_id: str,
    ) -> PositionCloseOutcome:
        if not operation_id.strip():
            raise ValueError("position close operation id cannot be empty")
        executed_at_ms = time.time_ns() // 1_000_000
        exposure = context.instance.exposure
        legs: list[PairExecutionLeg] = []
        btc_quote = Decimal(str(exposure.btc_long))
        eth_quote = Decimal(str(exposure.eth_short))
        if btc_quote > 0:
            legs.append(
                PairExecutionLeg(
                    symbol="BTCUSDT",
                    direction=PairDirection.LONG,
                    action=PairLegAction.CLOSE,
                    fill=NormalizedTradeFill(
                        identity=f"{operation_id}:btc-long-close",
                        executed_at_ms=executed_at_ms,
                        quote_volume=btc_quote,
                        symbol="BTCUSDT",
                        position_action="close",
                        maker=True,
                        source="mock_execution",
                    ),
                )
            )
        if eth_quote > 0:
            legs.append(
                PairExecutionLeg(
                    symbol="ETHUSDT",
                    direction=PairDirection.SHORT,
                    action=PairLegAction.CLOSE,
                    fill=NormalizedTradeFill(
                        identity=f"{operation_id}:eth-short-close",
                        executed_at_ms=executed_at_ms,
                        quote_volume=eth_quote,
                        symbol="ETHUSDT",
                        position_action="close",
                        maker=True,
                        source="mock_execution",
                    ),
                )
            )
        return PositionCloseOutcome(
            status=CycleExecutionStatus.COMPLETED,
            reason="mock_positions_closed",
            legs=tuple(legs),
        )

    async def cancel_active_orders(self, context: AccountTelemetryContext) -> CancelOrdersOutcome:
        del context
        return CancelOrdersOutcome(verified=True, canceled_count=0, reason="no_active_orders")

    async def aclose(self) -> None:
        return None


class MockPairedExecutionAdapterFactory:
    def create(self, instance_id: str) -> PairedExecutionAdapter:
        return MockPairedExecutionAdapter()


class InMemoryExecutionJournal:
    def __init__(self) -> None:
        self._records: dict[str, ExecutionRecord] = {}
        self._sequences: dict[tuple[str, int], str] = {}
        self._lock = RLock()

    def begin(self, instance_id: str, plan: PairCyclePlan) -> BeginCycleResult:
        with self._lock:
            existing_id = self._sequences.get((instance_id, plan.sequence))
            if existing_id is not None:
                return BeginCycleResult(self._records[existing_id], False)
            now_ms = time.time_ns() // 1_000_000
            record = ExecutionRecord(
                instance_id=instance_id,
                plan=plan,
                status=CycleExecutionStatus.PLANNED,
                reason="prepared_before_submit",
                created_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
            self._records[plan.cycle_id] = record
            self._sequences[(instance_id, plan.sequence)] = plan.cycle_id
            return BeginCycleResult(record, True)

    def finish(
        self,
        cycle_id: str,
        status: CycleExecutionStatus,
        reason: str,
    ) -> ExecutionRecord:
        if status is CycleExecutionStatus.PLANNED:
            raise ExecutionStateError("execution cycle cannot finish as planned")
        _require_reason_code(reason)
        with self._lock:
            current = self._records.get(cycle_id)
            if current is None:
                raise KeyError(cycle_id)
            if current.status in _TERMINAL_STATUSES:
                if current.status is status and current.reason == reason:
                    return current
                raise ExecutionStateError("terminal execution cycle cannot change outcome")
            if current.status is CycleExecutionStatus.OPENED and status is CycleExecutionStatus.OPENED:
                if current.reason == reason:
                    return current
                raise ExecutionStateError("opened execution cycle cannot be opened again")
            updated = ExecutionRecord(
                instance_id=current.instance_id,
                plan=current.plan,
                status=status,
                reason=reason,
                created_at_ms=current.created_at_ms,
                updated_at_ms=time.time_ns() // 1_000_000,
            )
            self._records[cycle_id] = updated
            return updated

    def find(self, instance_id: str, sequence: int) -> ExecutionRecord | None:
        with self._lock:
            cycle_id = self._sequences.get((instance_id, sequence))
            return self._records.get(cycle_id) if cycle_id is not None else None

    def list_recent(self, instance_id: str, limit: int) -> list[ExecutionRecord]:
        with self._lock:
            records = [record for record in self._records.values() if record.instance_id == instance_id]
            records.sort(key=lambda record: record.plan.sequence, reverse=True)
            return records[:limit]

    def recover_incomplete(self) -> int:
        with self._lock:
            pending = [
                record.plan.cycle_id
                for record in self._records.values()
                if record.status in {CycleExecutionStatus.PLANNED, CycleExecutionStatus.OPENED}
            ]
        for cycle_id in pending:
            current = self._records[cycle_id]
            self.finish(
                cycle_id,
                CycleExecutionStatus.UNCERTAIN,
                (
                    "process_restarted_with_open_pair"
                    if current.status is CycleExecutionStatus.OPENED
                    else "process_restarted_before_terminal_result"
                ),
            )
        return len(pending)

    def remove(self, instance_id: str) -> None:
        with self._lock:
            cycle_ids = [
                cycle_id
                for (candidate_id, _sequence), cycle_id in self._sequences.items()
                if candidate_id == instance_id
            ]
            for cycle_id in cycle_ids:
                record = self._records.pop(cycle_id)
                self._sequences.pop((record.instance_id, record.plan.sequence), None)

    def close(self) -> None:
        return None


class SQLiteExecutionJournal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_cycles (
                cycle_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                total_quote TEXT NOT NULL,
                btc_long_quote TEXT NOT NULL,
                eth_short_quote TEXT NOT NULL,
                allocation_version TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                turnover_quote TEXT NOT NULL DEFAULT '0',
                position_hold_seconds INTEGER NOT NULL DEFAULT 0,
                round_interval_seconds INTEGER NOT NULL DEFAULT 0,
                sizing_mode TEXT NOT NULL DEFAULT 'legacy_fixed',
                strategy_id TEXT NOT NULL DEFAULT 'legacy',
                UNIQUE(instance_id, sequence),
                FOREIGN KEY(instance_id) REFERENCES instances(id) ON DELETE CASCADE
            )
            """
        )
        self._migrate_plan_columns()
        self._connection.commit()
        self._lock = RLock()

    def begin(self, instance_id: str, plan: PairCyclePlan) -> BeginCycleResult:
        now_ms = time.time_ns() // 1_000_000
        with self._lock, self._connection:
            existing = self._select(instance_id, plan.sequence)
            if existing is not None:
                return BeginCycleResult(existing, False)
            self._connection.execute(
                """
                INSERT INTO execution_cycles(
                    cycle_id, instance_id, sequence, total_quote, btc_long_quote,
                    eth_short_quote, allocation_version, status, reason, created_at_ms, updated_at_ms,
                    turnover_quote, position_hold_seconds, round_interval_seconds, sizing_mode, strategy_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.cycle_id,
                    instance_id,
                    plan.sequence,
                    str(plan.total_quote),
                    str(plan.btc_long_quote),
                    str(plan.eth_short_quote),
                    plan.allocation_version,
                    CycleExecutionStatus.PLANNED.value,
                    "prepared_before_submit",
                    now_ms,
                    now_ms,
                    str(plan.turnover_quote),
                    plan.position_hold_seconds,
                    plan.round_interval_seconds,
                    plan.sizing_mode,
                    plan.strategy_id,
                ),
            )
        record = self.find(instance_id, plan.sequence)
        if record is None:
            raise ExecutionStateError("created execution cycle could not be reloaded")
        return BeginCycleResult(record, True)

    def finish(
        self,
        cycle_id: str,
        status: CycleExecutionStatus,
        reason: str,
    ) -> ExecutionRecord:
        if status is CycleExecutionStatus.PLANNED:
            raise ExecutionStateError("execution cycle cannot finish as planned")
        _require_reason_code(reason)
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM execution_cycles WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchone()
            if row is None:
                raise KeyError(cycle_id)
            current = self._record(row)
            if current.status in _TERMINAL_STATUSES:
                if current.status is status and current.reason == reason:
                    return current
                raise ExecutionStateError("terminal execution cycle cannot change outcome")
            if current.status is CycleExecutionStatus.OPENED and status is CycleExecutionStatus.OPENED:
                if current.reason == reason:
                    return current
                raise ExecutionStateError("opened execution cycle cannot be opened again")
            self._connection.execute(
                """
                UPDATE execution_cycles
                SET status = ?, reason = ?, updated_at_ms = ?
                WHERE cycle_id = ?
                """,
                (status.value, reason, time.time_ns() // 1_000_000, cycle_id),
            )
        record = self.find(current.instance_id, current.plan.sequence)
        assert record is not None
        return record

    def find(self, instance_id: str, sequence: int) -> ExecutionRecord | None:
        with self._lock:
            return self._select(instance_id, sequence)

    def list_recent(self, instance_id: str, limit: int) -> list[ExecutionRecord]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM execution_cycles
                WHERE instance_id = ?
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (instance_id, limit),
            ).fetchall()
        return [self._record(row) for row in rows]

    def recover_incomplete(self) -> int:
        with self._lock, self._connection:
            now_ms = time.time_ns() // 1_000_000
            planned = self._connection.execute(
                """
                UPDATE execution_cycles
                SET status = ?, reason = ?, updated_at_ms = ?
                WHERE status = ?
                """,
                (
                    CycleExecutionStatus.UNCERTAIN.value,
                    "process_restarted_before_terminal_result",
                    now_ms,
                    CycleExecutionStatus.PLANNED.value,
                ),
            ).rowcount
            opened = self._connection.execute(
                """
                UPDATE execution_cycles
                SET status = ?, reason = ?, updated_at_ms = ?
                WHERE status = ?
                """,
                (
                    CycleExecutionStatus.UNCERTAIN.value,
                    "process_restarted_with_open_pair",
                    now_ms,
                    CycleExecutionStatus.OPENED.value,
                ),
            ).rowcount
            return planned + opened

    def remove(self, instance_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM execution_cycles WHERE instance_id = ?", (instance_id,))

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _select(self, instance_id: str, sequence: int) -> ExecutionRecord | None:
        row = self._connection.execute(
            "SELECT * FROM execution_cycles WHERE instance_id = ? AND sequence = ?",
            (instance_id, sequence),
        ).fetchone()
        return self._record(row) if row is not None else None

    def _migrate_plan_columns(self) -> None:
        existing = {str(row[1]) for row in self._connection.execute("PRAGMA table_info(execution_cycles)")}
        additions = {
            "turnover_quote": "TEXT NOT NULL DEFAULT '0'",
            "position_hold_seconds": "INTEGER NOT NULL DEFAULT 0",
            "round_interval_seconds": "INTEGER NOT NULL DEFAULT 0",
            "sizing_mode": "TEXT NOT NULL DEFAULT 'legacy_fixed'",
            "strategy_id": "TEXT NOT NULL DEFAULT 'legacy'",
        }
        for column, definition in additions.items():
            if column not in existing:
                self._connection.execute(f"ALTER TABLE execution_cycles ADD COLUMN {column} {definition}")

    @staticmethod
    def _record(row: tuple[object, ...]) -> ExecutionRecord:
        plan = PairCyclePlan(
            cycle_id=str(row[0]),
            sequence=int(row[2]),
            total_quote=Decimal(str(row[3])),
            btc_long_quote=Decimal(str(row[4])),
            eth_short_quote=Decimal(str(row[5])),
            allocation_version=str(row[6]),
            turnover_quote=(
                Decimal(str(row[11])) if len(row) > 11 and Decimal(str(row[11])) > 0 else Decimal(str(row[3])) * 2
            ),
            position_hold_seconds=int(row[12]) if len(row) > 12 else 0,
            round_interval_seconds=int(row[13]) if len(row) > 13 else 0,
            sizing_mode=str(row[14]) if len(row) > 14 else "legacy_fixed",
            strategy_id=str(row[15]) if len(row) > 15 else "legacy",
        )
        return ExecutionRecord(
            instance_id=str(row[1]),
            plan=plan,
            status=CycleExecutionStatus(str(row[7])),
            reason=str(row[8]),
            created_at_ms=int(row[9]),
            updated_at_ms=int(row[10]),
        )


class PairedCycleCoordinator:
    def __init__(
        self,
        journal: ExecutionJournal,
        ledger: TradeVolumeLedger,
        allocation_provider: PairAllocationProvider,
        adapter_factory: PairedExecutionAdapterFactory,
        *,
        total_quote: Decimal,
        rng: random.Random | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not total_quote.is_finite() or total_quote <= 0:
            raise ValueError("mock cycle total quote must be finite and positive")
        self._journal = journal
        self._ledger = ledger
        self._allocation_provider = allocation_provider
        self._adapter_factory = adapter_factory
        self._total_quote = total_quote
        self._rng = rng or random.SystemRandom()
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._adapters: dict[str, PairedExecutionAdapter] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def execute_next(self, context: AccountTelemetryContext) -> CycleExecutionResult | None:
        if context.instance.status is not InstanceStatus.RUNNING:
            raise ExecutionStateError("pair cycle requires a running instance")
        instance_id = context.instance.id
        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            sequence = context.instance.cycle.completed + 1
            existing = self._journal.find(instance_id, sequence)
            if existing is not None:
                if existing.status is CycleExecutionStatus.PLANNED:
                    existing = self._journal.finish(
                        existing.plan.cycle_id,
                        CycleExecutionStatus.UNCERTAIN,
                        "existing_planned_cycle_not_resubmitted",
                    )
                elif existing.status is CycleExecutionStatus.OPENED:
                    return await self._close_if_due(context, existing)
                return CycleExecutionResult(existing, False, 0)
            next_action_at_ms = context.instance.strategy_progress.next_action_at_ms
            if next_action_at_ms is not None and self._clock_ms() < next_action_at_ms:
                return None
            allocation = await self._allocation_provider.get(context)
            sizing = plan_strategy_cycle(
                context.instance.strategy,
                target_progress_quote(context.instance),
                allocation,
                self._rng,
            )
            plan = PairCyclePlan(
                cycle_id=f"cycle-{uuid4().hex}",
                sequence=sequence,
                total_quote=sizing.total_open_quote,
                btc_long_quote=sizing.btc_long_quote,
                eth_short_quote=sizing.eth_short_quote,
                allocation_version=allocation.version,
                turnover_quote=sizing.turnover_quote,
                position_hold_seconds=random_seconds(
                    context.instance.strategy.position_hold_min_seconds,
                    context.instance.strategy.position_hold_max_seconds,
                    self._rng,
                ),
                round_interval_seconds=random_seconds(
                    context.instance.strategy.round_interval_min_seconds,
                    context.instance.strategy.round_interval_max_seconds,
                    self._rng,
                ),
                sizing_mode=sizing.sizing_mode,
                strategy_id=context.instance.strategy.id,
            )
            begun = self._journal.begin(instance_id, plan)
            if not begun.created:
                record = begun.record
                if record.status is CycleExecutionStatus.PLANNED:
                    record = self._journal.finish(
                        record.plan.cycle_id,
                        CycleExecutionStatus.UNCERTAIN,
                        "existing_planned_cycle_not_resubmitted",
                    )
                return CycleExecutionResult(record, False, 0)

            adapter = self._adapters.get(instance_id)
            if adapter is None:
                adapter = self._adapter_factory.create(instance_id)
                self._adapters[instance_id] = adapter
            try:
                outcome = await adapter.open_once(context, begun.record.plan)
                self._validate_outcome(
                    begun.record.plan,
                    outcome,
                    expected_status=CycleExecutionStatus.OPENED,
                    expected_action=PairLegAction.OPEN,
                )
                inserted = (
                    self._record_fills(context, tuple(leg.fill for leg in outcome.legs))
                    if outcome.status is CycleExecutionStatus.OPENED
                    else 0
                )
                record = self._journal.finish(
                    begun.record.plan.cycle_id,
                    outcome.status,
                    outcome.reason,
                )
                return CycleExecutionResult(record, True, inserted)
            except Exception as exc:
                record = self._journal.finish(
                    begun.record.plan.cycle_id,
                    CycleExecutionStatus.UNCERTAIN,
                    f"adapter_exception:{type(exc).__name__.lower()}",
                )
                return CycleExecutionResult(record, True, 0)

    async def check_allocation(self, context: AccountTelemetryContext) -> None:
        await self._allocation_provider.get(context)

    async def cancel_active_orders(self, context: AccountTelemetryContext) -> CancelOrdersOutcome:
        instance_id = context.instance.id
        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            adapter = self._adapters.get(instance_id)
            if adapter is None:
                adapter = self._adapter_factory.create(instance_id)
                self._adapters[instance_id] = adapter
            return await adapter.cancel_active_orders(context)

    async def close_positions(
        self,
        context: AccountTelemetryContext,
    ) -> PositionCloseExecutionResult:
        if context.instance.status is InstanceStatus.RUNNING:
            raise ExecutionStateError("positions cannot be closed while the strategy is running")
        exposure = context.instance.exposure
        if exposure.btc_long <= 0 and exposure.eth_short <= 0:
            raise ExecutionStateError("the instance has no open positions")

        instance_id = context.instance.id
        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            sequence = context.instance.cycle.completed + 1
            existing = self._journal.find(instance_id, sequence)
            active_cycle_id = context.instance.strategy_progress.active_cycle_id
            active_record: ExecutionRecord | None = None
            has_cycle_projection = (
                active_cycle_id is not None or context.instance.strategy_progress.stage is StrategyStage.HOLDING
            )
            if existing is not None or has_cycle_projection:
                if (
                    existing is None
                    or existing.status is not CycleExecutionStatus.OPENED
                    or active_cycle_id is None
                    or existing.plan.cycle_id != active_cycle_id
                ):
                    raise ExecutionStateError("position snapshot does not match one opened pair cycle")
                active_record = existing

            operation_id = (
                active_record.plan.cycle_id
                if active_record is not None
                else self._snapshot_close_operation_id(context.instance.id, context.instance.cycle.completed, exposure)
            )
            adapter = self._adapters.get(instance_id)
            if adapter is None:
                adapter = self._adapter_factory.create(instance_id)
                self._adapters[instance_id] = adapter
            try:
                outcome = await adapter.close_positions_once(context, operation_id)
                self._validate_position_close_outcome(exposure, outcome)
                inserted = (
                    self._record_fills(context, tuple(leg.fill for leg in outcome.legs))
                    if outcome.status is CycleExecutionStatus.COMPLETED
                    else 0
                )
                record = (
                    self._journal.finish(active_record.plan.cycle_id, outcome.status, outcome.reason)
                    if active_record is not None
                    else None
                )
                return PositionCloseExecutionResult(outcome, record, True, inserted)
            except Exception as exc:
                reason = f"adapter_exception:{type(exc).__name__.lower()}"[:80]
                record = (
                    self._journal.finish(
                        active_record.plan.cycle_id,
                        CycleExecutionStatus.UNCERTAIN,
                        reason,
                    )
                    if active_record is not None
                    else None
                )
                return PositionCloseExecutionResult(
                    PositionCloseOutcome(CycleExecutionStatus.UNCERTAIN, reason),
                    record,
                    True,
                    0,
                )

    async def reconcile_manual_pair_closed(
        self,
        context: AccountTelemetryContext,
    ) -> CycleExecutionResult:
        instance_id = context.instance.id
        lock = self._locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            sequence = context.instance.cycle.completed + 1
            record = self._journal.find(instance_id, sequence)
            active_cycle_id = context.instance.strategy_progress.active_cycle_id
            if (
                record is None
                or record.status is not CycleExecutionStatus.OPENED
                or active_cycle_id is None
                or record.plan.cycle_id != active_cycle_id
            ):
                raise ExecutionStateError("manual close does not match one opened pair cycle")
            completed = self._journal.finish(
                record.plan.cycle_id,
                CycleExecutionStatus.COMPLETED,
                "manual_pair_closed",
            )
            return CycleExecutionResult(completed, True, 0)

    async def _close_if_due(
        self,
        context: AccountTelemetryContext,
        opened: ExecutionRecord,
    ) -> CycleExecutionResult:
        close_due_at_ms = opened.updated_at_ms + opened.plan.position_hold_seconds * 1_000
        if self._clock_ms() < close_due_at_ms:
            return CycleExecutionResult(opened, False, 0)
        adapter = self._adapters.get(context.instance.id)
        if adapter is None:
            adapter = self._adapter_factory.create(context.instance.id)
            self._adapters[context.instance.id] = adapter
        try:
            outcome = await adapter.close_once(context, opened.plan)
            self._validate_outcome(
                opened.plan,
                outcome,
                expected_status=CycleExecutionStatus.COMPLETED,
                expected_action=PairLegAction.CLOSE,
            )
            inserted = (
                self._record_fills(context, tuple(leg.fill for leg in outcome.legs))
                if outcome.status is CycleExecutionStatus.COMPLETED
                else 0
            )
            record = self._journal.finish(opened.plan.cycle_id, outcome.status, outcome.reason)
            return CycleExecutionResult(record, True, inserted)
        except Exception as exc:
            record = self._journal.finish(
                opened.plan.cycle_id,
                CycleExecutionStatus.UNCERTAIN,
                f"adapter_exception:{type(exc).__name__.lower()}",
            )
            return CycleExecutionResult(record, True, 0)

    async def reset_instance(self, instance_id: str) -> None:
        adapter = self._adapters.pop(instance_id, None)
        if adapter is not None:
            await asyncio.gather(adapter.aclose(), return_exceptions=True)

    async def remove_instance(self, instance_id: str) -> None:
        await self.reset_instance(instance_id)
        self._locks.pop(instance_id, None)
        self._journal.remove(instance_id)

    async def close(self) -> None:
        adapters = tuple(self._adapters.values())
        self._adapters.clear()
        self._locks.clear()
        await asyncio.gather(
            *(adapter.aclose() for adapter in adapters),
            self._allocation_provider.aclose(),
            return_exceptions=True,
        )

    def _record_fills(
        self,
        context: AccountTelemetryContext,
        fills: tuple[NormalizedTradeFill, ...],
    ) -> int:
        inserted = self._ledger.record_account_fills(
            context.instance.id,
            context.instance.mode.value,
            fills,
        )
        self._ledger.refresh_sessions(
            context.instance.id,
            context.instance.mode.value,
            now_ms=max((fill.executed_at_ms for fill in fills), default=self._clock_ms()),
            source_complete=True,
            stale=False,
        )
        return inserted

    @staticmethod
    def _validate_outcome(
        plan: PairCyclePlan,
        outcome: PairExecutionOutcome,
        *,
        expected_status: CycleExecutionStatus,
        expected_action: PairLegAction,
    ) -> None:
        if outcome.status in {CycleExecutionStatus.REJECTED, CycleExecutionStatus.UNCERTAIN}:
            return
        if outcome.status is not expected_status:
            raise ExecutionStateError("execution adapter returned an unexpected pair phase")
        by_direction = {(leg.symbol, leg.direction, leg.action): leg for leg in outcome.legs}
        btc = by_direction.get(("BTCUSDT", PairDirection.LONG, expected_action))
        eth = by_direction.get(("ETHUSDT", PairDirection.SHORT, expected_action))
        if btc is None or eth is None or len(by_direction) != 2:
            raise ExecutionStateError("pair phase must contain BTC long and ETH short")
        if btc.fill.quote_volume != plan.btc_long_quote:
            raise ExecutionStateError("BTC long fill does not match pair plan")
        if eth.fill.quote_volume != plan.eth_short_quote:
            raise ExecutionStateError("ETH short fill does not match pair plan")

    @staticmethod
    def _validate_position_close_outcome(
        exposure: ExposureSnapshot,
        outcome: PositionCloseOutcome,
    ) -> None:
        if outcome.status in {CycleExecutionStatus.REJECTED, CycleExecutionStatus.UNCERTAIN}:
            return
        expected: dict[tuple[str, PairDirection, PairLegAction], Decimal] = {}
        btc_quote = Decimal(str(exposure.btc_long))
        eth_quote = Decimal(str(exposure.eth_short))
        if btc_quote > 0:
            expected[("BTCUSDT", PairDirection.LONG, PairLegAction.CLOSE)] = btc_quote
        if eth_quote > 0:
            expected[("ETHUSDT", PairDirection.SHORT, PairLegAction.CLOSE)] = eth_quote
        actual = {(leg.symbol, leg.direction, leg.action): leg.fill.quote_volume for leg in outcome.legs}
        if len(actual) != len(outcome.legs) or actual != expected:
            raise ExecutionStateError("position close fills do not match the current exposure snapshot")

    @staticmethod
    def _snapshot_close_operation_id(
        instance_id: str,
        completed_cycles: int,
        exposure: ExposureSnapshot,
    ) -> str:
        btc_quote = Decimal(str(exposure.btc_long)).normalize()
        eth_quote = Decimal(str(exposure.eth_short)).normalize()
        return f"snapshot-close:{instance_id}:{completed_cycles}:{btc_quote}:{eth_quote}"


def _require_reason_code(reason: str) -> None:
    if _REASON_CODE.fullmatch(reason) is None:
        raise ExecutionStateError("execution reason must be a sanitized reason code")

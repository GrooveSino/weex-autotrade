from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from decimal import Decimal
from uuid import uuid4

from .execution_contracts import (
    CancelOrdersOutcome,
    CycleExecutionResult,
    CycleExecutionStatus,
    ExecutionJournal,
    ExecutionRecord,
    ExecutionStateError,
    PairAllocationProvider,
    PairCyclePlan,
    PairedExecutionAdapter,
    PairedExecutionAdapterFactory,
    PairLegAction,
    PositionCloseExecutionResult,
    PositionCloseOutcome,
)
from .execution_validation import (
    record_fills,
    snapshot_close_operation_id,
    validate_outcome,
    validate_position_close_outcome,
)
from .models import InstanceStatus, StrategyStage
from .strategy import plan_strategy_cycle, random_seconds, target_progress_quote
from .telemetry import AccountTelemetryContext
from .volume_history import TradeVolumeLedger


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
                validate_outcome(
                    begun.record.plan,
                    outcome,
                    expected_status=CycleExecutionStatus.OPENED,
                    expected_action=PairLegAction.OPEN,
                )
                inserted = (
                    record_fills(self._ledger, self._clock_ms, context, tuple(leg.fill for leg in outcome.legs))
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
                else snapshot_close_operation_id(context.instance.id, context.instance.cycle.completed, exposure)
            )
            adapter = self._adapters.get(instance_id)
            if adapter is None:
                adapter = self._adapter_factory.create(instance_id)
                self._adapters[instance_id] = adapter
            try:
                outcome = await adapter.close_positions_once(context, operation_id)
                validate_position_close_outcome(exposure, outcome)
                inserted = (
                    record_fills(self._ledger, self._clock_ms, context, tuple(leg.fill for leg in outcome.legs))
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
            validate_outcome(
                opened.plan,
                outcome,
                expected_status=CycleExecutionStatus.COMPLETED,
                expected_action=PairLegAction.CLOSE,
            )
            inserted = (
                record_fills(self._ledger, self._clock_ms, context, tuple(leg.fill for leg in outcome.legs))
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

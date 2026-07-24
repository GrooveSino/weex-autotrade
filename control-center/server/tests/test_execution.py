import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from fleet_api.execution import (
    AllocationUnavailable,
    CycleExecutionStatus,
    InMemoryExecutionJournal,
    PairCyclePlan,
    PairedCycleCoordinator,
    SQLiteExecutionJournal,
)
from fleet_api.repository import SQLiteAccountRepository
from fleet_api.telemetry import AccountTelemetryContext
from fleet_api.volume_history import InMemoryTradeVolumeLedger

from .test_execution_support import (
    ControlledAdapter,
    CountingAllocationProvider,
    SingleAdapterFactory,
    UnavailableAllocationProvider,
    coordinator,
    running_account,
)


def test_completed_pair_latches_one_beta_allocation_for_open_and_close() -> None:
    async def scenario() -> None:
        target, journal, ledger, adapter, allocation = coordinator()
        context = AccountTelemetryContext(running_account(), None)

        opened = await target.execute_next(context)
        completed = await target.execute_next(context)
        repeated = await target.execute_next(context)

        assert opened.record.status is CycleExecutionStatus.OPENED
        assert completed.record.status is CycleExecutionStatus.COMPLETED
        assert completed.record.plan.btc_long_quote == Decimal("16")
        assert completed.record.plan.eth_short_quote == Decimal("4")
        assert completed.record.plan.turnover_quote == Decimal("40")
        assert opened.submitted is True
        assert opened.fills_inserted == 2
        assert completed.submitted is True
        assert completed.fills_inserted == 2
        assert repeated.record == completed.record
        assert repeated.submitted is False
        assert adapter.calls == 2
        assert adapter.open_calls == 1
        assert adapter.close_calls == 1
        assert allocation.calls == 1
        aggregate = ledger.aggregate(context.instance.id, 0)
        assert aggregate.lifetime == Decimal("40")
        assert aggregate.fill_count == 4
        assert journal.find(context.instance.id, 1) == completed.record
        assert journal.list_recent(context.instance.id, 20) == [completed.record]
        await target.close()

    asyncio.run(scenario())


def test_cancel_active_orders_delegates_once_and_returns_verified_outcome() -> None:
    async def scenario() -> None:
        target, _journal, _ledger, adapter, allocation = coordinator()
        context = AccountTelemetryContext(running_account(), None)

        outcome = await target.cancel_active_orders(context)

        assert outcome.verified is True
        assert outcome.canceled_count == 0
        assert outcome.reason == "no_active_orders"
        assert adapter.cancel_calls == 1
        assert allocation.calls == 0
        await target.close()

    asyncio.run(scenario())


def test_manual_pair_close_reconciles_only_the_matching_opened_cycle_without_submission() -> None:
    async def scenario() -> None:
        target, journal, _ledger, adapter, allocation = coordinator()
        plan = PairCyclePlan(
            cycle_id="cycle-manually-closed",
            sequence=1,
            total_quote=Decimal("20"),
            btc_long_quote=Decimal("16"),
            eth_short_quote=Decimal("4"),
            allocation_version="test-allocation-v1",
        )
        journal.begin("ins-execution", plan)
        journal.finish(plan.cycle_id, CycleExecutionStatus.OPENED, "mock_pair_opened")
        account = running_account().model_copy(
            update={
                "strategy_progress": running_account().strategy_progress.model_copy(
                    update={"active_cycle_id": plan.cycle_id}
                )
            },
            deep=True,
        )

        result = await target.reconcile_manual_pair_closed(AccountTelemetryContext(account, None))

        assert result.record.status is CycleExecutionStatus.COMPLETED
        assert result.record.reason == "manual_pair_closed"
        assert result.fills_inserted == 0
        assert adapter.calls == 0
        assert allocation.calls == 0
        await target.close()

    asyncio.run(scenario())


def test_opened_pair_is_not_closed_before_the_configured_hold_time() -> None:
    async def scenario() -> None:
        journal = InMemoryExecutionJournal()
        ledger = InMemoryTradeVolumeLedger()
        adapter = ControlledAdapter("complete")
        clock = [0]
        target = PairedCycleCoordinator(
            journal,
            ledger,
            CountingAllocationProvider(),
            SingleAdapterFactory(adapter),
            total_quote=Decimal("20"),
            clock_ms=lambda: clock[0],
        )
        account = running_account()
        account = account.model_copy(
            update={
                "strategy": account.strategy.model_copy(
                    update={"position_hold_min_seconds": 10, "position_hold_max_seconds": 10}
                )
            },
            deep=True,
        )
        context = AccountTelemetryContext(account, None)

        opened = await target.execute_next(context)
        clock[0] = opened.record.updated_at_ms + 9_999
        waiting = await target.execute_next(context)
        clock[0] += 1
        completed = await target.execute_next(context)

        assert opened.record.status is CycleExecutionStatus.OPENED
        assert waiting.record.status is CycleExecutionStatus.OPENED
        assert waiting.submitted is False
        assert adapter.close_calls == 1
        assert completed.record.status is CycleExecutionStatus.COMPLETED
        assert ledger.aggregate(account.id, 0).fill_count == 4
        await target.close()

    asyncio.run(scenario())


def test_post_only_rejection_is_terminal_and_never_resubmitted() -> None:
    async def scenario() -> None:
        target, _journal, ledger, adapter, allocation = coordinator("reject")
        context = AccountTelemetryContext(running_account(), None)

        rejected = await target.execute_next(context)
        repeated = await target.execute_next(context)

        assert rejected.record.status is CycleExecutionStatus.REJECTED
        assert rejected.record.reason == "post_only_rejected"
        assert repeated.submitted is False
        assert adapter.calls == 1
        assert allocation.calls == 1
        assert ledger.aggregate(context.instance.id, 0).fill_count == 0
        await target.close()

    asyncio.run(scenario())


def test_transport_exception_becomes_redacted_uncertain_and_never_resubmits() -> None:
    async def scenario() -> None:
        target, _journal, ledger, adapter, _allocation = coordinator("raise")
        context = AccountTelemetryContext(running_account(), None)

        uncertain = await target.execute_next(context)
        repeated = await target.execute_next(context)

        assert uncertain.record.status is CycleExecutionStatus.UNCERTAIN
        assert uncertain.record.reason == "adapter_exception:connectionerror"
        assert "secret transport" not in uncertain.record.reason
        assert repeated.submitted is False
        assert adapter.calls == 1
        assert ledger.aggregate(context.instance.id, 0).fill_count == 0
        await target.close()

    asyncio.run(scenario())


def test_close_transport_exception_is_uncertain_and_never_retried() -> None:
    async def scenario() -> None:
        target, _journal, ledger, adapter, allocation = coordinator("close_raise")
        context = AccountTelemetryContext(running_account(), None)

        opened = await target.execute_next(context)
        uncertain = await target.execute_next(context)
        repeated = await target.execute_next(context)

        assert opened.record.status is CycleExecutionStatus.OPENED
        assert uncertain.record.status is CycleExecutionStatus.UNCERTAIN
        assert uncertain.record.reason == "adapter_exception:connectionerror"
        assert repeated.submitted is False
        assert adapter.open_calls == 1
        assert adapter.close_calls == 1
        assert allocation.calls == 1
        assert ledger.aggregate(context.instance.id, 0).fill_count == 2
        await target.close()

    asyncio.run(scenario())


def test_allocation_unavailable_stops_before_plan_journal_or_submission() -> None:
    async def scenario() -> None:
        journal = InMemoryExecutionJournal()
        ledger = InMemoryTradeVolumeLedger()
        adapter = ControlledAdapter("complete")
        allocation = UnavailableAllocationProvider()
        target = PairedCycleCoordinator(
            journal,
            ledger,
            allocation,
            SingleAdapterFactory(adapter),
            total_quote=Decimal("20"),
        )
        context = AccountTelemetryContext(running_account(), None)

        with pytest.raises(AllocationUnavailable, match="^beta_unusable$"):
            await target.execute_next(context)

        assert allocation.calls == 1
        assert adapter.calls == 0
        assert journal.find(context.instance.id, 1) is None
        assert journal.list_recent(context.instance.id, 20) == []
        assert ledger.aggregate(context.instance.id, 0).fill_count == 0
        await target.close()

    asyncio.run(scenario())


def test_preexisting_planned_cycle_is_marked_uncertain_without_adapter_or_ratio_call() -> None:
    async def scenario() -> None:
        target, journal, _ledger, adapter, allocation = coordinator()
        context = AccountTelemetryContext(running_account(), None)
        plan = PairCyclePlan(
            cycle_id="cycle-before-restart",
            sequence=1,
            total_quote=Decimal("20"),
            btc_long_quote=Decimal("10"),
            eth_short_quote=Decimal("10"),
            allocation_version="test-existing-v1",
        )
        journal.begin(context.instance.id, plan)

        result = await target.execute_next(context)

        assert result.record.status is CycleExecutionStatus.UNCERTAIN
        assert result.record.reason == "existing_planned_cycle_not_resubmitted"
        assert result.submitted is False
        assert adapter.calls == 0
        assert allocation.calls == 0
        await target.close()

    asyncio.run(scenario())


def test_sqlite_journal_recovers_planned_cycle_and_cascades_with_account(tmp_path: Path) -> None:
    path = tmp_path / "fleet.db"
    repository = SQLiteAccountRepository(path)
    instance = running_account()
    repository.create(instance)
    plan = PairCyclePlan(
        cycle_id="cycle-sqlite-restart",
        sequence=1,
        total_quote=Decimal("20"),
        btc_long_quote=Decimal("10"),
        eth_short_quote=Decimal("10"),
        allocation_version="test-existing-v1",
    )
    first = SQLiteExecutionJournal(path)
    first.begin(instance.id, plan)
    first.close()

    restored = SQLiteExecutionJournal(path)
    assert restored.recover_incomplete() == 1
    record = restored.find(instance.id, 1)
    assert record is not None
    assert record.status is CycleExecutionStatus.UNCERTAIN
    assert record.reason == "process_restarted_before_terminal_result"

    repository.delete(instance.id)
    assert restored.find(instance.id, 1) is None
    assert restored.list_recent(instance.id, 20) == []
    restored.close()
    repository.close()


def test_sqlite_journal_marks_opened_pair_uncertain_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "fleet-opened.db"
    repository = SQLiteAccountRepository(path)
    instance = running_account("ins-opened-restart")
    repository.create(instance)
    plan = PairCyclePlan(
        cycle_id="cycle-opened-before-restart",
        sequence=1,
        total_quote=Decimal("20"),
        btc_long_quote=Decimal("16"),
        eth_short_quote=Decimal("4"),
        allocation_version="test-existing-v1",
        position_hold_seconds=600,
    )
    first = SQLiteExecutionJournal(path)
    first.begin(instance.id, plan)
    first.finish(plan.cycle_id, CycleExecutionStatus.OPENED, "mock_pair_opened")
    first.close()

    restored = SQLiteExecutionJournal(path)
    assert restored.recover_incomplete() == 1
    record = restored.find(instance.id, 1)
    assert record is not None
    assert record.status is CycleExecutionStatus.UNCERTAIN
    assert record.reason == "process_restarted_with_open_pair"
    restored.close()
    repository.close()

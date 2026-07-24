import asyncio
from decimal import Decimal

import pytest

from fleet_api.execution import (
    CancelOrdersOutcome,
    CycleExecutionStatus,
    InMemoryExecutionJournal,
    PairedCycleCoordinator,
)
from fleet_api.models import (
    CreateInstanceRequest,
    ExposureSnapshot,
    InstanceAction,
    InstanceStatus,
    StrategyStage,
)
from fleet_api.repository import InMemoryAccountRepository
from fleet_api.runtime import AccountRuntimeManager
from fleet_api.service import FleetControlService, UnsafeOperation
from fleet_api.telemetry import MockAccountTelemetryAdapterFactory
from fleet_api.vault import EphemeralCredentialVault
from fleet_api.volume_history import InMemoryTradeVolumeLedger

from .test_runtime_support import (
    CancelTrackingFactory,
    FixedExposureFactory,
    RecoveringAllocationProvider,
    StaticAllocationProvider,
    payload,
    seed_open_pair,
)


def test_unverified_pause_cancellation_enters_protected_error_until_stop_rechecks() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        service = FleetControlService(repository, EphemeralCredentialVault())
        instance = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("cancel-failure", "api-key-CANCEL", "user:proxy@proxy.example.com:9307")
            )
        )
        ledger = InMemoryTradeVolumeLedger()
        factory = CancelTrackingFactory({instance.id: CancelOrdersOutcome(False, 1, "open_orders_remaining")})
        execution = PairedCycleCoordinator(
            InMemoryExecutionJournal(),
            ledger,
            StaticAllocationProvider(),
            factory,
            total_quote=Decimal("20"),
        )
        runtime = AccountRuntimeManager(
            service,
            MockAccountTelemetryAdapterFactory(),
            ledger,
            execution,
            max_parallel_polls=2,
            poll_timeout_seconds=1,
        )
        await runtime.apply_action(instance.id, InstanceAction.START)

        with pytest.raises(UnsafeOperation, match="could not be verified"):
            await runtime.apply_action(instance.id, InstanceAction.PAUSE)

        failed = service.get_instance(instance.id)
        assert failed.status is InstanceStatus.ERROR
        assert failed.strategy_progress.system_pause_reason == "cancel_unverified:manual_pause"
        assert failed.runtime.last_error_type == "OrderCancellationUnverified"
        await runtime.poll_all()
        assert service.get_instance(instance.id).status is InstanceStatus.ERROR

        factory.adapters[instance.id].outcome = CancelOrdersOutcome(True, 0, "no_active_orders")
        stopped = await runtime.apply_action(instance.id, InstanceAction.STOP)
        assert stopped.status is InstanceStatus.STOPPED
        assert stopped.strategy_progress.system_pause_reason is None
        assert factory.adapters[instance.id].cancel_calls == 2
        await runtime.close()

    asyncio.run(scenario())


def test_global_stop_reports_each_verified_and_unverified_account() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        service = FleetControlService(repository, EphemeralCredentialVault())
        first = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("global-ok", "api-key-GLOBAL1", "user:proxy@proxy.example.com:9308")
            )
        )
        second = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("global-fail", "api-key-GLOBAL2", "user:proxy@proxy.example.com:9309")
            )
        )
        ledger = InMemoryTradeVolumeLedger()
        factory = CancelTrackingFactory(
            {
                first.id: CancelOrdersOutcome(True, 3, "all_orders_canceled"),
                second.id: CancelOrdersOutcome(False, 1, "open_orders_remaining"),
            }
        )
        execution = PairedCycleCoordinator(
            InMemoryExecutionJournal(),
            ledger,
            StaticAllocationProvider(),
            factory,
            total_quote=Decimal("20"),
        )
        runtime = AccountRuntimeManager(
            service,
            MockAccountTelemetryAdapterFactory(),
            ledger,
            execution,
            max_parallel_polls=2,
            poll_timeout_seconds=1,
        )
        await runtime.apply_action(first.id, InstanceAction.START)
        await runtime.apply_action(second.id, InstanceAction.START)

        with pytest.raises(UnsafeOperation, match="confirmation mismatch"):
            await runtime.stop_all("stop all")
        result = await runtime.stop_all("STOP ALL")

        assert result.stopped == 1
        assert result.cancel_verified == 1
        assert result.cancel_failed == 1
        assert service.get_instance(first.id).status is InstanceStatus.STOPPED
        assert service.get_instance(second.id).status is InstanceStatus.ERROR
        assert factory.adapters[first.id].cancel_calls == 1
        assert factory.adapters[second.id].cancel_calls == 1
        await runtime.close()

    asyncio.run(scenario())


def test_manual_full_pair_close_is_reconciled_without_submitting_another_close() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        service = FleetControlService(repository, EphemeralCredentialVault())
        instance = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("manual-close", "api-key-MANUAL", "user:proxy@proxy.example.com:9310")
            )
        )
        service.apply_action(instance.id, InstanceAction.START)
        ledger = InMemoryTradeVolumeLedger()
        ledger.set_complete(instance.id, True)
        journal = InMemoryExecutionJournal()
        plan = seed_open_pair(service, journal, instance.id)
        cancel_factory = CancelTrackingFactory({instance.id: CancelOrdersOutcome(True, 1, "all_orders_canceled")})
        execution = PairedCycleCoordinator(
            journal,
            ledger,
            StaticAllocationProvider(),
            cancel_factory,
            total_quote=Decimal("20"),
        )
        runtime = AccountRuntimeManager(
            service,
            FixedExposureFactory(ExposureSnapshot()),
            ledger,
            execution,
            max_parallel_polls=2,
            poll_timeout_seconds=1,
        )

        await runtime.poll_all()

        reconciled = service.get_instance(instance.id)
        record = journal.find(instance.id, 1)
        assert record is not None
        assert record.status is CycleExecutionStatus.COMPLETED
        assert record.reason == "manual_pair_closed"
        assert record.plan == plan
        assert reconciled.status is InstanceStatus.RUNNING
        assert reconciled.strategy_progress.stage is StrategyStage.COOLDOWN
        assert reconciled.cycle.completed == 1
        assert reconciled.exposure == ExposureSnapshot()
        assert "人工双腿平仓" in reconciled.phase
        assert cancel_factory.adapters[instance.id].cancel_calls == 1
        assert ledger.aggregate(instance.id, 0).fill_count == 0
        await runtime.close()

    asyncio.run(scenario())


def test_manual_single_leg_close_pauses_and_never_auto_repairs_the_pair() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        service = FleetControlService(repository, EphemeralCredentialVault())
        instance = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("single-leg", "api-key-SINGLE", "user:proxy@proxy.example.com:9311")
            )
        )
        service.apply_action(instance.id, InstanceAction.START)
        ledger = InMemoryTradeVolumeLedger()
        journal = InMemoryExecutionJournal()
        seed_open_pair(service, journal, instance.id)
        cancel_factory = CancelTrackingFactory({instance.id: CancelOrdersOutcome(True, 1, "all_orders_canceled")})
        execution = PairedCycleCoordinator(
            journal,
            ledger,
            StaticAllocationProvider(),
            cancel_factory,
            total_quote=Decimal("20"),
        )
        runtime = AccountRuntimeManager(
            service,
            FixedExposureFactory(ExposureSnapshot(btc_long=16, eth_short=0)),
            ledger,
            execution,
            max_parallel_polls=2,
            poll_timeout_seconds=1,
        )

        await runtime.poll_all()

        paused = service.get_instance(instance.id)
        record = journal.find(instance.id, 1)
        assert paused.status is InstanceStatus.PAUSED
        assert paused.strategy_progress.system_pause_reason == "position:eth_leg_missing"
        assert "eth_leg_missing" in paused.phase
        assert record is not None
        assert record.status is CycleExecutionStatus.OPENED
        assert cancel_factory.adapters[instance.id].cancel_calls == 1
        assert ledger.aggregate(instance.id, 0).fill_count == 0
        assert await runtime.reconcile_beta_availability(True) == 0
        assert service.get_instance(instance.id).status is InstanceStatus.PAUSED
        await runtime.close()

    asyncio.run(scenario())


def test_unavailable_allocation_pauses_cancels_and_recovers_before_next_cycle() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        vault = EphemeralCredentialVault()
        service = FleetControlService(repository, vault)
        instance = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("recovering", "api-key-RECOVER", "user:proxy@proxy.example.com:9305")
            )
        )
        service.apply_action(instance.id, InstanceAction.START)
        ledger = InMemoryTradeVolumeLedger()
        ledger.set_complete(instance.id, True)
        journal = InMemoryExecutionJournal()
        allocation = RecoveringAllocationProvider()
        adapter_factory = CancelTrackingFactory({instance.id: CancelOrdersOutcome(True, 0, "no_active_orders")})
        execution = PairedCycleCoordinator(
            journal,
            ledger,
            allocation,
            adapter_factory,
            total_quote=Decimal("20"),
        )
        runtime = AccountRuntimeManager(
            service,
            MockAccountTelemetryAdapterFactory(),
            ledger,
            execution,
            max_parallel_polls=2,
            poll_timeout_seconds=1,
        )

        assert await runtime.poll_all() is True
        unavailable = service.get_instance(instance.id)
        first_metrics = runtime.metrics()
        assert unavailable.status is InstanceStatus.PAUSED
        assert unavailable.phase == "Beta 服务异常，系统已暂停 (beta_timeout)"
        assert unavailable.strategy_progress.system_pause_reason == "beta:beta_timeout"
        assert unavailable.runtime.last_error_type is None
        assert unavailable.cycle.completed == 0
        assert journal.find(instance.id, 1) is None
        assert journal.list_recent(instance.id, 20) == []
        assert ledger.aggregate(instance.id, 0).fill_count == 0
        assert adapter_factory.adapters[instance.id].cancel_calls == 1
        assert first_metrics.successful_polls == 1
        assert first_metrics.failed_polls == 0

        assert await runtime.poll_all() is True
        resumed = service.get_instance(instance.id)
        assert resumed.status is InstanceStatus.RUNNING
        assert resumed.strategy_progress.system_pause_reason is None
        assert resumed.strategy_progress.stage.value == "idle"
        assert journal.find(instance.id, 1) is None
        assert ledger.aggregate(instance.id, 0).fill_count == 0

        assert await runtime.poll_all() is True
        opened = service.get_instance(instance.id)
        assert opened.strategy_progress.stage.value == "holding"
        assert opened.cycle.completed == 0
        assert ledger.aggregate(instance.id, 0).fill_count == 2

        assert await runtime.poll_all() is True
        recovered = service.get_instance(instance.id)
        record = journal.find(instance.id, 1)
        assert recovered.status is InstanceStatus.RUNNING
        assert recovered.cycle.completed == 1
        assert record is not None
        assert record.status is CycleExecutionStatus.COMPLETED
        assert record.plan.allocation_version == "test-allocation-v1"
        assert ledger.aggregate(instance.id, 0).fill_count == 4
        assert allocation.calls == 3
        await runtime.close()

    asyncio.run(scenario())

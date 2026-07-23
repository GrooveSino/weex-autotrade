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
    MixedFactory,
    RecordingFactory,
    StaticAllocationProvider,
    payload,
    seed_open_pair,
)


def test_stopped_instance_can_close_current_positions_and_reconcile_the_open_cycle() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        service = FleetControlService(repository, EphemeralCredentialVault())
        instance = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("manual-close", "api-key-CLOSE", "user:proxy@proxy.example.com:9312")
            )
        )
        ledger = InMemoryTradeVolumeLedger()
        ledger.set_complete(instance.id, True)
        journal = InMemoryExecutionJournal()
        plan = seed_open_pair(service, journal, instance.id)
        close_factory = CancelTrackingFactory({instance.id: CancelOrdersOutcome(True, 2, "all_orders_canceled")})
        execution = PairedCycleCoordinator(
            journal,
            ledger,
            StaticAllocationProvider(),
            close_factory,
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

        closed = await runtime.close_positions(instance.id)

        record = journal.find(instance.id, 1)
        assert record is not None
        assert record.status is CycleExecutionStatus.COMPLETED
        assert record.reason == "mock_positions_closed"
        assert record.plan == plan
        assert closed.status is InstanceStatus.STOPPED
        assert closed.exposure == ExposureSnapshot()
        assert closed.strategy_progress.stage is StrategyStage.COOLDOWN
        assert closed.strategy_progress.active_cycle_id is None
        assert closed.strategy_progress.generated_volume_quote == Decimal("20")
        assert closed.cycle.completed == 1
        assert closed.volume.lifetime == 20
        assert closed.volume.today == 20
        assert close_factory.adapters[instance.id].cancel_calls == 1
        aggregate = ledger.aggregate(instance.id, 0)
        assert aggregate.lifetime == Decimal("20")
        assert aggregate.fill_count == 2
        await runtime.close()

    asyncio.run(scenario())

def test_close_positions_flattens_a_single_remaining_mock_leg_without_fabricating_a_fill() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        service = FleetControlService(repository, EphemeralCredentialVault())
        instance = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("single-close", "api-key-ONELEG", "user:proxy@proxy.example.com:9314")
            )
        )
        service.repository.replace(
            instance.model_copy(
                update={"exposure": ExposureSnapshot(btc_long=12, eth_short=0)},
                deep=True,
            )
        )
        ledger = InMemoryTradeVolumeLedger()
        journal = InMemoryExecutionJournal()
        close_factory = CancelTrackingFactory({instance.id: CancelOrdersOutcome(True, 0, "no_active_orders")})
        execution = PairedCycleCoordinator(
            journal,
            ledger,
            StaticAllocationProvider(),
            close_factory,
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

        closed = await runtime.close_positions(instance.id)

        assert closed.exposure == ExposureSnapshot()
        assert closed.strategy_progress.stage is StrategyStage.IDLE
        assert closed.strategy_progress.generated_volume_quote == Decimal("12")
        assert closed.cycle.completed == 0
        aggregate = ledger.aggregate(instance.id, 0)
        assert aggregate.lifetime == Decimal("12")
        assert aggregate.fill_count == 1
        await runtime.close()

    asyncio.run(scenario())

def test_close_positions_preserves_exposure_when_order_cancellation_is_unverified() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        service = FleetControlService(repository, EphemeralCredentialVault())
        instance = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("unsafe-close", "api-key-UNSAFE", "user:proxy@proxy.example.com:9313")
            )
        )
        service.repository.replace(
            instance.model_copy(
                update={"exposure": ExposureSnapshot(btc_long=12, eth_short=8)},
                deep=True,
            )
        )
        ledger = InMemoryTradeVolumeLedger()
        journal = InMemoryExecutionJournal()
        close_factory = CancelTrackingFactory({instance.id: CancelOrdersOutcome(False, 0, "orders_still_active")})
        execution = PairedCycleCoordinator(
            journal,
            ledger,
            StaticAllocationProvider(),
            close_factory,
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

        with pytest.raises(UnsafeOperation, match="could not be verified"):
            await runtime.close_positions(instance.id)

        protected = service.get_instance(instance.id)
        assert protected.status is InstanceStatus.ERROR
        assert protected.exposure == ExposureSnapshot(btc_long=12, eth_short=8)
        assert protected.strategy_progress.system_pause_reason == "cancel_unverified:manual_position_close"
        assert ledger.aggregate(instance.id, 0).fill_count == 0
        assert close_factory.adapters[instance.id].cancel_calls == 1
        await runtime.close()

    asyncio.run(scenario())

def test_runtime_polls_accounts_concurrently_with_account_scoped_credentials_and_proxies() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        vault = EphemeralCredentialVault()
        service = FleetControlService(repository, vault)
        first = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("first", "api-key-FIRST", "user-a:proxy-a@proxy.example.com:9101")
            )
        )
        second = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("second", "api-key-SECOND", "user-b:proxy-b@proxy.example.com:9102")
            )
        )
        factory = RecordingFactory()
        runtime = AccountRuntimeManager(
            service,
            factory,
            InMemoryTradeVolumeLedger(),
            max_parallel_polls=2,
            poll_timeout_seconds=1,
        )

        assert await runtime.poll_all() is True
        assert factory.max_active == 2
        assert factory.contexts[first.id].credentials is not None
        assert factory.contexts[first.id].credentials.api_key.get_secret_value() == "api-key-FIRST"
        assert factory.contexts[first.id].credentials.proxy_url.get_secret_value().endswith(":9101")
        assert factory.contexts[second.id].credentials is not None
        assert factory.contexts[second.id].credentials.api_key.get_secret_value() == "api-key-SECOND"
        assert factory.contexts[second.id].credentials.proxy_url.get_secret_value().endswith(":9102")
        first_health = service.get_instance(first.id).runtime
        metrics = runtime.metrics()
        assert first_health.last_poll_succeeded_at_ms is not None
        assert first_health.last_poll_duration_ms is not None
        assert first_health.consecutive_failures == 0
        assert metrics.last_round_account_count == 2
        assert metrics.last_round_succeeded == 2
        assert metrics.last_round_failed == 0
        await runtime.close()

    asyncio.run(scenario())

def test_stopped_runtime_failure_is_isolated_and_error_detail_is_redacted() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        vault = EphemeralCredentialVault()
        service = FleetControlService(repository, vault)
        failing = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("failing", "api-key-FAIL", "user-a:private-a@proxy.example.com:9201")
            )
        )
        healthy = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("healthy", "api-key-OK", "user-b:private-b@proxy.example.com:9202")
            )
        )
        runtime = AccountRuntimeManager(
            service,
            MixedFactory(failing.id),
            InMemoryTradeVolumeLedger(),
            max_parallel_polls=2,
            poll_timeout_seconds=1,
        )

        assert await runtime.poll_all() is True
        assert await runtime.poll_all() is True
        failing_snapshot = service.get_instance(failing.id)
        healthy_snapshot = service.get_instance(healthy.id)
        assert failing_snapshot.status is InstanceStatus.STOPPED
        assert failing_snapshot.phase == "已停止；数据待核验 (RuntimeError)"
        assert failing_snapshot.runtime.consecutive_failures == 2
        assert failing_snapshot.runtime.last_error_type == "RuntimeError"
        assert failing_snapshot.runtime.last_poll_failed_at_ms is not None
        assert healthy_snapshot.wallet.equity > 0
        assert healthy_snapshot.runtime.last_poll_succeeded_at_ms is not None
        metrics = runtime.metrics()
        assert metrics.poll_rounds == 2
        assert metrics.successful_polls == 2
        assert metrics.failed_polls == 2
        assert metrics.last_round_succeeded == 1
        assert metrics.last_round_failed == 1
        serialized_logs = " ".join(line.message for line in service.logs(failing.id, 20))
        assert "RuntimeError" in serialized_logs
        assert "api-key-FAIL" not in serialized_logs
        assert "private-a" not in serialized_logs
        await runtime.close()

    asyncio.run(scenario())

def test_legacy_unprotected_telemetry_error_migrates_to_stopped_on_the_next_poll() -> None:
    repository = InMemoryAccountRepository()
    service = FleetControlService(repository, EphemeralCredentialVault())
    instance = service.create_instance(
        CreateInstanceRequest.model_validate(
            payload("legacy-telemetry", "api-key-LEGACY", "user:proxy@proxy.example.com:9202")
        )
    )
    repository.replace(
        instance.model_copy(
            update={
                "status": InstanceStatus.ERROR,
                "phase": "数据同步失败 (NetworkError)",
                "runtime": instance.runtime.model_copy(update={"last_error_type": "NetworkError"}),
            },
            deep=True,
        )
    )

    migrated = service.record_runtime_failure(instance.id, "NetworkError")

    assert migrated.status is InstanceStatus.STOPPED
    assert migrated.phase == "已停止；数据待核验 (NetworkError)"

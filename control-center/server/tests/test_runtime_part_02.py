import asyncio
from decimal import Decimal

from fastapi.testclient import TestClient

from fleet_api.config import ControlPlaneSettings
from fleet_api.execution import (
    CancelOrdersOutcome,
    CycleExecutionStatus,
    InMemoryExecutionJournal,
    MockPairedExecutionAdapterFactory,
    PairedCycleCoordinator,
)
from fleet_api.main import create_app
from fleet_api.models import (
    CreateInstanceRequest,
    InstanceAction,
    InstanceStatus,
)
from fleet_api.repository import InMemoryAccountRepository
from fleet_api.runtime import AccountRuntimeManager
from fleet_api.service import FleetControlService
from fleet_api.telemetry import MockAccountTelemetryAdapterFactory
from fleet_api.vault import EphemeralCredentialVault
from fleet_api.volume_history import InMemoryTradeVolumeLedger

from .test_runtime_support import (
    AllFailingFactory,
    CancelTrackingFactory,
    RecordingFactory,
    StaticAllocationProvider,
    payload,
)


def test_paused_and_running_runtime_failures_preserve_control_state_or_halt_execution() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        service = FleetControlService(repository, EphemeralCredentialVault())
        paused = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("paused-failure", "api-key-PAUSED", "user:proxy@proxy.example.com:9203")
            )
        )
        running = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("running-failure", "api-key-RUNNING", "user:proxy@proxy.example.com:9204")
            )
        )
        for instance_id, status, phase in (
            (paused.id, InstanceStatus.PAUSED, "已人工暂停"),
            (running.id, InstanceStatus.RUNNING, "Mock 成交量策略运行中"),
        ):
            current = service.get_instance(instance_id)
            service.repository.replace(
                current.model_copy(
                    update={
                        "status": status,
                        "phase": phase,
                        "cycle": current.cycle.model_copy(update={"next_action_at": "等待规划"}),
                    },
                    deep=True,
                )
            )

        runtime = AccountRuntimeManager(
            service,
            AllFailingFactory(),
            InMemoryTradeVolumeLedger(),
            max_parallel_polls=2,
            poll_timeout_seconds=1,
        )

        assert await runtime.poll_all() is True
        paused_snapshot = service.get_instance(paused.id)
        running_snapshot = service.get_instance(running.id)
        assert paused_snapshot.status is InstanceStatus.PAUSED
        assert paused_snapshot.cycle.next_action_at is None
        assert running_snapshot.status is InstanceStatus.WARNING
        assert running_snapshot.cycle.next_action_at is None
        assert running_snapshot.phase == "运行已安全暂停；数据待核验 (RuntimeError)"
        await runtime.close()

    asyncio.run(scenario())

def test_repeated_stop_after_verified_cancellation_is_idempotent_even_after_telemetry_failure() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        service = FleetControlService(repository, EphemeralCredentialVault())
        instance = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("idempotent-stop", "api-key-STOP", "user:proxy@proxy.example.com:9205")
            )
        )
        factory = CancelTrackingFactory({instance.id: CancelOrdersOutcome(True, 0, "execution_disabled")})
        execution = PairedCycleCoordinator(
            InMemoryExecutionJournal(),
            InMemoryTradeVolumeLedger(),
            StaticAllocationProvider(),
            factory,
            total_quote=Decimal("20"),
        )
        runtime = AccountRuntimeManager(
            service,
            MockAccountTelemetryAdapterFactory(),
            InMemoryTradeVolumeLedger(),
            execution,
            max_parallel_polls=2,
            poll_timeout_seconds=1,
        )

        first, duplicate = await asyncio.gather(
            runtime.apply_action(instance.id, InstanceAction.STOP),
            runtime.apply_action(instance.id, InstanceAction.STOP),
        )
        assert first.status is InstanceStatus.STOPPED
        assert duplicate.status is InstanceStatus.STOPPED
        assert factory.adapters[instance.id].cancel_calls == 1
        assert first.runtime.last_stop_verified_at_ms is not None

        service.record_runtime_failure(instance.id, "NetworkError")
        retry = await runtime.apply_action(instance.id, InstanceAction.STOP)
        assert retry.status is InstanceStatus.STOPPED
        assert factory.adapters[instance.id].cancel_calls == 1
        messages = [line.message for line in service.logs(instance.id, 20)]
        assert sum(message.startswith("实例操作已接受：停止") for message in messages) == 1
        assert sum(message.startswith("撤单核验完成：") for message in messages) == 1
        await runtime.close()

    asyncio.run(scenario())

def test_runtime_caps_parallelism_for_a_forty_account_fleet() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        vault = EphemeralCredentialVault()
        service = FleetControlService(repository, vault)
        instance_ids: list[str] = []
        for index in range(40):
            created = service.create_instance(
                CreateInstanceRequest.model_validate(
                    payload(
                        f"fleet-{index:02d}",
                        f"api-key-{index:02d}",
                        f"user-{index}:proxy-{index}@proxy.example.com:{9500 + index}",
                    )
                )
            )
            instance_ids.append(created.id)
            service.apply_action(created.id, InstanceAction.START)
        factory = RecordingFactory()
        ledger = InMemoryTradeVolumeLedger()
        journal = InMemoryExecutionJournal()
        execution = PairedCycleCoordinator(
            journal,
            ledger,
            StaticAllocationProvider(),
            MockPairedExecutionAdapterFactory(),
            total_quote=Decimal("20"),
        )
        runtime = AccountRuntimeManager(
            service,
            factory,
            ledger,
            execution,
            max_parallel_polls=8,
            poll_timeout_seconds=1,
        )

        assert await runtime.poll_all() is True
        assert len(factory.contexts) == 40
        assert factory.max_active == 8
        assert all(journal.find(instance_id, 1).status is CycleExecutionStatus.OPENED for instance_id in instance_ids)
        assert await runtime.poll_all() is True
        assert all(service.get_instance(instance_id).cycle.completed == 1 for instance_id in instance_ids)
        assert all(journal.find(instance_id, 1) is not None for instance_id in instance_ids)
        metrics = runtime.metrics()
        assert metrics.max_parallel_polls == 8
        assert metrics.max_observed_parallelism == 8
        assert metrics.accounts_polled == 80
        assert metrics.successful_polls == 80
        assert metrics.failed_polls == 0
        assert metrics.last_round_account_count == 40
        assert metrics.last_round_succeeded == 40
        assert metrics.last_round_duration_ms is not None
        await runtime.close()

    asyncio.run(scenario())

def test_manual_refresh_never_advances_btc_long_eth_short_execution_cycle() -> None:
    allocation = StaticAllocationProvider()
    app = create_app(
        ControlPlaneSettings(seed_demo_data=False, mock_tick_interval_seconds=60),
        allocation_provider=allocation,
    )
    with TestClient(app) as api:
        instance_id = api.post(
            "/api/v1/instances",
            json=payload("paired", "api-key-PAIR", "user:proxy@proxy.example.com:9301"),
        ).json()["id"]
        assert api.post(f"/api/v1/instances/{instance_id}/actions/start").status_code == 200

        refreshed = api.post(f"/api/v1/instances/{instance_id}/refresh")
        ledger_aggregate = app.state.trade_volume_ledger.aggregate(instance_id, 0)
        execution_record = app.state.execution_journal.find(instance_id, 1)

    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["cycle"]["completed"] == 0
    assert body["volume"]["lifetime"] == 0
    assert body["volume"]["complete"] is True
    assert ledger_aggregate.fill_count == 0
    assert execution_record is None
    assert allocation.calls == 0
    assert body["volume"]["lifetime"] == float(ledger_aggregate.lifetime)
    assert body["exposure"]["btcLong"] == body["exposure"]["ethShort"] == 0

def test_manual_pause_ends_incremental_run_and_explicit_start_begins_a_fresh_run() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        service = FleetControlService(repository, EphemeralCredentialVault())
        instance = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("manual-pause", "api-key-PAUSE", "user:proxy@proxy.example.com:9306")
            )
        )
        ledger = InMemoryTradeVolumeLedger()
        factory = CancelTrackingFactory({instance.id: CancelOrdersOutcome(True, 2, "all_orders_canceled")})
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

        started = await runtime.apply_action(instance.id, InstanceAction.START)
        started_at_ms = started.strategy_progress.started_at_ms
        paused = await runtime.apply_action(instance.id, InstanceAction.PAUSE)

        assert paused.status is InstanceStatus.PAUSED
        assert paused.strategy_progress.system_pause_reason is None
        assert paused.strategy_progress.started_at_ms == started_at_ms
        assert factory.adapters[instance.id].cancel_calls == 1
        assert await runtime.reconcile_beta_availability(True) == 0
        assert service.get_instance(instance.id).status is InstanceStatus.PAUSED

        resumed = await runtime.apply_action(instance.id, InstanceAction.START)
        assert resumed.status is InstanceStatus.RUNNING
        assert resumed.strategy_progress.started_at_ms is not None
        assert resumed.strategy_progress.started_at_ms >= started_at_ms
        assert resumed.strategy_progress.generated_volume_quote == 0
        await runtime.close()

    asyncio.run(scenario())

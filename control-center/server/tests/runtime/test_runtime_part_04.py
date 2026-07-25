import asyncio
import time
from decimal import Decimal

from fastapi.testclient import TestClient

from fleet_api.accounts.repository import InMemoryAccountRepository
from fleet_api.auth.vault import EphemeralCredentialVault
from fleet_api.config.config import ControlPlaneSettings
from fleet_api.execution import (
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
    VolumeStrategyInput,
)
from fleet_api.runtime.runtime import AccountRuntimeManager
from fleet_api.runtime.telemetry import MockAccountTelemetryAdapterFactory
from fleet_api.services.control.service import FleetControlService
from fleet_api.volume.core.volume_history import InMemoryTradeVolumeLedger

from ..support.test_runtime_support import (
    StaticAllocationProvider,
    payload,
)


def test_scheduled_mock_poll_executes_one_persisted_pair_cycle() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        vault = EphemeralCredentialVault()
        service = FleetControlService(repository, vault)
        instance = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("scheduled", "api-key-SCHEDULED", "user:proxy@proxy.example.com:9303")
            )
        )
        service.apply_action(instance.id, InstanceAction.START)
        ledger = InMemoryTradeVolumeLedger()
        ledger.set_complete(instance.id, True)
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
            MockAccountTelemetryAdapterFactory(),
            ledger,
            execution,
            max_parallel_polls=2,
            poll_timeout_seconds=1,
        )

        assert await runtime.poll_all() is True
        opened = service.get_instance(instance.id)
        assert opened.cycle.completed == 0
        assert opened.strategy_progress.stage.value == "holding"
        assert opened.volume.lifetime == 20
        assert opened.exposure.btc_long == 16
        assert opened.exposure.eth_short == 4
        assert journal.find(instance.id, 1).status is CycleExecutionStatus.OPENED

        assert await runtime.poll_all() is True
        snapshot = service.get_instance(instance.id)
        aggregate = ledger.aggregate(instance.id, 0)
        logs = service.logs(instance.id, 20)

        assert snapshot.cycle.completed == 1
        assert snapshot.volume.lifetime == 40
        assert snapshot.strategy_progress.generated_volume_quote == Decimal("40")
        assert snapshot.exposure.btc_long == 0
        assert snapshot.exposure.eth_short == 0
        assert aggregate.fill_count == 4
        assert journal.find(instance.id, 1).status is CycleExecutionStatus.COMPLETED
        assert any("已开仓" in line.message for line in logs)
        assert any("已平仓" in line.message for line in logs)
        await runtime.close()

    asyncio.run(scenario())


def test_strategy_waits_for_hold_and_round_interval_before_advancing() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        vault = EphemeralCredentialVault()
        service = FleetControlService(repository, vault)
        request = payload("timed", "api-key-TIMED", "user:proxy@proxy.example.com:9308")
        strategy = service.create_strategy(
            VolumeStrategyInput.model_validate(
                {
                    "name": "timed strategy",
                    "targetVolumeQuote": "80",
                    "roundTurnoverQuoteMin": "40",
                    "roundTurnoverQuoteMax": "40",
                    "positionHoldMinSeconds": 10,
                    "positionHoldMaxSeconds": 10,
                    "roundIntervalMinSeconds": 20,
                    "roundIntervalMaxSeconds": 20,
                }
            )
        )
        request["strategyId"] = strategy.id
        instance = service.create_instance(CreateInstanceRequest.model_validate(request))
        service.apply_action(instance.id, InstanceAction.START)
        ledger = InMemoryTradeVolumeLedger()
        ledger.set_complete(instance.id, True)
        journal = InMemoryExecutionJournal()
        clock = [time.time_ns() // 1_000_000]
        execution = PairedCycleCoordinator(
            journal,
            ledger,
            StaticAllocationProvider(),
            MockPairedExecutionAdapterFactory(),
            total_quote=Decimal("20"),
            clock_ms=lambda: clock[0],
        )
        runtime = AccountRuntimeManager(
            service,
            MockAccountTelemetryAdapterFactory(),
            ledger,
            execution,
            max_parallel_polls=2,
            poll_timeout_seconds=1,
        )

        await runtime.poll_all()
        opened_record = journal.find(instance.id, 1)
        assert opened_record is not None
        assert opened_record.status is CycleExecutionStatus.OPENED
        assert service.get_instance(instance.id).strategy_progress.stage.value == "holding"

        clock[0] = opened_record.updated_at_ms + 9_999
        await runtime.poll_all()
        assert journal.find(instance.id, 1).status is CycleExecutionStatus.OPENED
        assert ledger.aggregate(instance.id, 0).fill_count == 2

        clock[0] = opened_record.updated_at_ms + 10_000
        await runtime.poll_all()
        completed = service.get_instance(instance.id)
        assert journal.find(instance.id, 1).status is CycleExecutionStatus.COMPLETED
        assert completed.strategy_progress.generated_volume_quote == Decimal("40")
        assert completed.strategy_progress.stage.value == "cooldown"
        assert completed.strategy_progress.next_action_at_ms is not None

        clock[0] = completed.strategy_progress.next_action_at_ms - 1
        await runtime.poll_all()
        assert journal.find(instance.id, 2) is None

        clock[0] = completed.strategy_progress.next_action_at_ms
        await runtime.poll_all()
        assert journal.find(instance.id, 2).status is CycleExecutionStatus.OPENED
        await runtime.close()

    asyncio.run(scenario())


def test_scheduler_uses_each_accounts_independent_mock_cycle_quote() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        vault = EphemeralCredentialVault()
        service = FleetControlService(repository, vault)
        first_payload = payload("quote-a", "api-key-QUOTE-A", "user:proxy@proxy.example.com:9311")
        first_payload["mockCycleTotalQuote"] = "12.50"
        second_payload = payload("quote-b", "api-key-QUOTE-B", "user:proxy@proxy.example.com:9312")
        second_payload["mockCycleTotalQuote"] = "37.50"
        first = service.create_instance(CreateInstanceRequest.model_validate(first_payload))
        second = service.create_instance(CreateInstanceRequest.model_validate(second_payload))
        service.apply_action(first.id, InstanceAction.START)
        service.apply_action(second.id, InstanceAction.START)

        ledger = InMemoryTradeVolumeLedger()
        ledger.set_complete(first.id, True)
        ledger.set_complete(second.id, True)
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
            MockAccountTelemetryAdapterFactory(),
            ledger,
            execution,
            max_parallel_polls=2,
            poll_timeout_seconds=1,
        )

        assert await runtime.poll_all() is True
        first_record = journal.find(first.id, 1)
        second_record = journal.find(second.id, 1)

        assert first_record.plan.total_quote == Decimal("12.50")
        assert first_record.plan.btc_long_quote == Decimal("10.000")
        assert first_record.plan.eth_short_quote == Decimal("2.500")
        assert second_record.plan.total_quote == Decimal("37.50")
        assert second_record.plan.btc_long_quote == Decimal("30.000")
        assert second_record.plan.eth_short_quote == Decimal("7.500")
        assert ledger.aggregate(first.id, 0).lifetime == Decimal("12.500")
        assert ledger.aggregate(second.id, 0).lifetime == Decimal("37.500")
        await runtime.close()

    asyncio.run(scenario())


def test_runtime_stops_exactly_at_incremental_target_and_allows_a_fresh_run() -> None:
    async def scenario() -> None:
        repository = InMemoryAccountRepository()
        vault = EphemeralCredentialVault()
        service = FleetControlService(repository, vault)
        instance = service.create_instance(
            CreateInstanceRequest.model_validate(
                payload("target", "api-key-TARGET", "user:proxy@proxy.example.com:9304")
            )
        )
        instance = instance.model_copy(
            update={
                "cycle": instance.cycle.model_copy(update={"target": 1}),
                "strategy": instance.strategy.model_copy(update={"target_volume_quote": Decimal("40")}),
            },
            deep=True,
        )
        repository.replace(instance)
        service.apply_action(instance.id, InstanceAction.START)
        ledger = InMemoryTradeVolumeLedger()
        ledger.set_complete(instance.id, True)
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
            MockAccountTelemetryAdapterFactory(),
            ledger,
            execution,
            max_parallel_polls=2,
            poll_timeout_seconds=1,
        )

        await runtime.poll_all()
        await runtime.poll_all()
        stopped = service.get_instance(instance.id)

        assert stopped.status is InstanceStatus.STOPPED
        assert stopped.phase == "目标交易量已完成"
        assert stopped.cycle.completed == 1
        assert journal.find(instance.id, 1) is not None
        assert journal.find(instance.id, 2) is None
        assert stopped.strategy_progress.generated_volume_quote == Decimal("40")
        restarted = service.apply_action(instance.id, InstanceAction.START)
        assert restarted.status is InstanceStatus.RUNNING
        assert restarted.strategy_progress.generated_volume_quote == 0
        assert restarted.strategy_progress.started_at_ms is not None
        await runtime.close()

    asyncio.run(scenario())


def test_mock_adapter_never_returns_fake_telemetry_for_live_account() -> None:
    live_payload = payload("live", "api-key-LIVE", "user:proxy@proxy.example.com:9302")
    live_payload["mode"] = "live"
    app = create_app(ControlPlaneSettings(seed_demo_data=False, mock_tick_interval_seconds=60))
    with TestClient(app) as api:
        instance_id = api.post("/api/v1/instances", json=live_payload).json()["id"]
        refreshed = api.post(f"/api/v1/instances/{instance_id}/refresh")
        snapshot = api.get(f"/api/v1/instances/{instance_id}").json()

    assert refreshed.status_code == 503
    assert refreshed.json()["detail"] == "telemetry unavailable (MockLiveTelemetryUnavailable)"
    assert snapshot["status"] == "stopped"
    assert snapshot["phase"] == "可启动已绑定策略"
    assert snapshot["executionLifecycle"]["state"] == "idle"
    assert snapshot["executionLifecycle"]["primaryAction"] == "start"
    assert snapshot["runtime"]["lastErrorType"] == "MockLiveTelemetryUnavailable"
    assert snapshot["wallet"]["equity"] == 0
    assert snapshot["volume"]["complete"] is False


def test_seeded_mock_volume_is_migrated_to_ledger_without_manual_execution() -> None:
    app = create_app(ControlPlaneSettings(mock_tick_interval_seconds=60))
    with TestClient(app) as api:
        before = api.get("/api/v1/instances/ins-api-01").json()
        refreshed = api.post("/api/v1/instances/ins-api-01/refresh").json()
        aggregate = app.state.trade_volume_ledger.aggregate("ins-api-01", 0)

    assert refreshed["volume"]["lifetime"] == before["volume"]["lifetime"]
    assert refreshed["volume"]["lifetime"] == float(aggregate.lifetime)
    assert aggregate.fill_count == 2
    assert aggregate.complete is True

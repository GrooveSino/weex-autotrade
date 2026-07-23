import asyncio
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from fleet_api.config import ControlPlaneSettings
from fleet_api.execution import (
    AllocationUnavailable,
    CancelOrdersOutcome,
    CycleExecutionStatus,
    InMemoryExecutionJournal,
    MockPairedExecutionAdapter,
    MockPairedExecutionAdapterFactory,
    PairAllocation,
    PairCyclePlan,
    PairedCycleCoordinator,
)
from fleet_api.main import create_app
from fleet_api.models import (
    CreateInstanceRequest,
    ExposureSnapshot,
    InstanceAction,
    InstanceStatus,
    ProxyStatus,
    StrategyStage,
    VolumeSnapshot,
    VolumeStrategyInput,
    WalletSnapshot,
)
from fleet_api.repository import InMemoryAccountRepository
from fleet_api.runtime import AccountRuntimeManager
from fleet_api.service import FleetControlService, UnsafeOperation
from fleet_api.telemetry import AccountTelemetry, AccountTelemetryContext, MockAccountTelemetryAdapterFactory
from fleet_api.vault import EphemeralCredentialVault
from fleet_api.volume_history import InMemoryTradeVolumeLedger


def payload(name: str, api_key: str, proxy_url: str) -> dict[str, object]:
    return {
        "name": name,
        "accountTag": "runtime",
        "mode": "demo",
        "credentials": {
            "apiKey": api_key,
            "apiSecret": f"secret-{name}",
            "passphrase": f"passphrase-{name}",
        },
        "proxy": {"type": "https", "url": proxy_url},
    }


class RecordingAdapter:
    def __init__(self, factory: "RecordingFactory", instance_id: str) -> None:
        self.factory = factory
        self.instance_id = instance_id

    async def collect(self, context: AccountTelemetryContext) -> AccountTelemetry:
        self.factory.active += 1
        self.factory.max_active = max(self.factory.max_active, self.factory.active)
        try:
            await asyncio.sleep(0.01)
            self.factory.contexts[self.instance_id] = context
            marker = float(len(self.factory.contexts))
            return AccountTelemetry(
                wallet=WalletSnapshot(equity=marker, available=marker),
                volume=VolumeSnapshot(lifetime=marker, today=marker, complete=True),
                exposure=ExposureSnapshot(),
                cycle_completed=context.instance.cycle.completed,
                proxy_status=ProxyStatus.HEALTHY,
                proxy_latency_ms=70,
                proxy_location="test adapter",
                phase="telemetry synchronized",
            )
        finally:
            self.factory.active -= 1

    async def aclose(self) -> None:
        return None


class RecordingFactory:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.contexts: dict[str, AccountTelemetryContext] = {}

    def create(self, instance_id: str) -> RecordingAdapter:
        return RecordingAdapter(self, instance_id)


class FixedExposureAdapter:
    def __init__(self, exposure: ExposureSnapshot) -> None:
        self.exposure = exposure

    async def collect(self, context: AccountTelemetryContext) -> AccountTelemetry:
        instance = context.instance
        return AccountTelemetry(
            wallet=instance.wallet,
            volume=instance.volume,
            exposure=self.exposure,
            cycle_completed=instance.cycle.completed,
            proxy_status=ProxyStatus.HEALTHY,
            proxy_latency_ms=50,
            proxy_location="fixed exposure",
            phase="position synchronized",
        )

    async def aclose(self) -> None:
        return None


class FixedExposureFactory:
    def __init__(self, exposure: ExposureSnapshot) -> None:
        self.exposure = exposure

    def create(self, instance_id: str) -> FixedExposureAdapter:
        del instance_id
        return FixedExposureAdapter(self.exposure)


class FailingAdapter(RecordingAdapter):
    async def collect(self, context: AccountTelemetryContext) -> AccountTelemetry:
        assert context.credentials is not None
        api_key = context.credentials.api_key.get_secret_value()
        proxy_url = context.credentials.proxy_url.get_secret_value()
        raise RuntimeError(f"must-not-leak:{api_key}:{proxy_url}")


class MixedFactory(RecordingFactory):
    def __init__(self, failing_id: str) -> None:
        super().__init__()
        self.failing_id = failing_id

    def create(self, instance_id: str) -> RecordingAdapter:
        if instance_id == self.failing_id:
            return FailingAdapter(self, instance_id)
        return super().create(instance_id)


class AllFailingFactory(RecordingFactory):
    def create(self, instance_id: str) -> RecordingAdapter:
        return FailingAdapter(self, instance_id)


class StaticAllocationProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self, context: AccountTelemetryContext) -> PairAllocation:
        del context
        self.calls += 1
        return PairAllocation(Decimal("0.8"), Decimal("0.2"), "test-allocation-v1")

    async def aclose(self) -> None:
        return None


class RecoveringAllocationProvider(StaticAllocationProvider):
    async def get(self, context: AccountTelemetryContext) -> PairAllocation:
        if self.calls == 0:
            self.calls += 1
            raise AllocationUnavailable("beta_timeout")
        return await super().get(context)


class CancelTrackingAdapter(MockPairedExecutionAdapter):
    def __init__(self, outcome: CancelOrdersOutcome) -> None:
        self.outcome = outcome
        self.cancel_calls = 0

    async def cancel_active_orders(self, context: AccountTelemetryContext) -> CancelOrdersOutcome:
        del context
        self.cancel_calls += 1
        return self.outcome


class CancelTrackingFactory:
    def __init__(self, outcomes: dict[str, CancelOrdersOutcome]) -> None:
        self.outcomes = outcomes
        self.adapters: dict[str, CancelTrackingAdapter] = {}

    def create(self, instance_id: str) -> CancelTrackingAdapter:
        adapter = self.adapters.get(instance_id)
        if adapter is None:
            adapter = CancelTrackingAdapter(self.outcomes[instance_id])
            self.adapters[instance_id] = adapter
        return adapter


def seed_open_pair(
    service: FleetControlService,
    journal: InMemoryExecutionJournal,
    instance_id: str,
) -> PairCyclePlan:
    plan = PairCyclePlan(
        cycle_id=f"cycle-open-{instance_id}",
        sequence=1,
        total_quote=Decimal("20"),
        btc_long_quote=Decimal("16"),
        eth_short_quote=Decimal("4"),
        allocation_version="test-allocation-v1",
    )
    journal.begin(instance_id, plan)
    journal.finish(plan.cycle_id, CycleExecutionStatus.OPENED, "mock_pair_opened")
    instance = service.get_instance(instance_id)
    service.repository.replace(
        instance.model_copy(
            update={
                "exposure": ExposureSnapshot(btc_long=16, eth_short=4),
                "strategy_progress": instance.strategy_progress.model_copy(
                    update={
                        "stage": StrategyStage.HOLDING,
                        "active_cycle_id": plan.cycle_id,
                    }
                ),
            },
            deep=True,
        )
    )
    return plan


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
        assert unavailable.phase == "Beta 服务异常，系统已暂停：beta_timeout"
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
    assert snapshot["phase"] == "已停止；数据待核验 (MockLiveTelemetryUnavailable)"
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


def test_manual_refresh_returns_sanitized_adapter_failure() -> None:
    factory = MixedFactory("pending")
    app = create_app(
        ControlPlaneSettings(seed_demo_data=False, mock_tick_interval_seconds=60),
        adapter_factory=factory,
    )
    with TestClient(app, raise_server_exceptions=False) as api:
        instance_id = api.post(
            "/api/v1/instances",
            json=payload("failing", "api-key-FAIL", "user:private@proxy.example.com:9401"),
        ).json()["id"]
        factory.failing_id = instance_id
        response = api.post(f"/api/v1/instances/{instance_id}/refresh")

    assert response.status_code == 503
    assert response.json()["detail"] == "telemetry unavailable (RuntimeError)"
    assert "api-key-FAIL" not in response.text
    assert "private" not in response.text

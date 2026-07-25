from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fleet_api.models import AccountInstance, InstanceStatus, ProxySnapshot, ProxyType, TradingMode
from fleet_api.trade_history_scheduler import ACTIVE_EVENT, FINAL_SESSION, TradeHistorySyncScheduler
from fleet_api.volume_contracts import TradeHistorySyncResult, TradeVolumeAggregate
from fleet_api.volume_history import InMemoryTradeVolumeLedger


def account(
    instance_id: str,
    *,
    status: InstanceStatus = InstanceStatus.STOPPED,
    proxy: str = "shared:8080",
) -> AccountInstance:
    return AccountInstance(
        id=instance_id,
        name=instance_id,
        account_tag=instance_id,
        api_key_tail="ABCD",
        mode=TradingMode.LIVE,
        status=status,
        phase="idle",
        proxy=ProxySnapshot(type=ProxyType.HTTP, host=proxy),
    )


@dataclass
class FakeService:
    instances: dict[str, AccountInstance]

    def list_instances(self) -> list[AccountInstance]:
        return list(self.instances.values())

    def get_instance(self, instance_id: str) -> AccountInstance:
        return self.instances[instance_id]


class FakeRuntime:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.fail = fail

    async def sync_history_step(self, instance_id: str) -> TradeHistorySyncResult:
        self.calls.append(instance_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        if self.fail:
            raise TimeoutError("fake history timeout")
        return TradeHistorySyncResult(
            aggregate=TradeVolumeAggregate(lifetime=0, today=0, fill_count=0, complete=True),
            pages_fetched=1,
            fills_inserted=0,
            stop_reason="history_exhausted",
            next_cursor=None,
        )


def scheduler(
    service: FakeService,
    runtime: FakeRuntime,
    ledger: InMemoryTradeVolumeLedger,
) -> TradeHistorySyncScheduler:
    return TradeHistorySyncScheduler(
        service,
        runtime,  # type: ignore[arg-type]
        ledger,
        is_active=lambda instance: instance.status is InstanceStatus.RUNNING,
        active_fallback_seconds=60,
    )


def test_new_account_initial_baseline_runs_once_then_becomes_silent() -> None:
    async def scenario() -> None:
        instance = account("initial")
        service = FakeService({instance.id: instance})
        ledger = InMemoryTradeVolumeLedger()
        runtime = FakeRuntime()
        subject = scheduler(service, runtime, ledger)

        subject.queue_initial_baseline(instance)
        assert await subject.run_due() is True
        assert await subject.run_due() is False
        assert runtime.calls == [instance.id]
        checkpoint = ledger.sync_checkpoint(instance.id, "live") or {}
        assert checkpoint["initial_baseline_state"] == "complete"
        assert checkpoint["pending"] is False

    asyncio.run(scenario())


def test_explicit_prepare_resumes_a_pending_initial_baseline() -> None:
    instance = account("pending")
    service = FakeService({instance.id: instance})
    ledger = InMemoryTradeVolumeLedger()
    ledger.save_sync_checkpoint(
        instance.id,
        "live",
        cursor="scan-1-1",
        pending=False,
        stale=True,
        scan_state={"pending_windows": [[20, 30]]},
        sync_reason="initial_baseline",
        initial_baseline_state="pending",
    )
    subject = scheduler(service, FakeRuntime(), ledger)

    subject.resume_initial_baseline(instance)

    checkpoint = ledger.sync_checkpoint(instance.id, "live") or {}
    assert checkpoint["initial_baseline_state"] == "queued"
    assert checkpoint["pending"] is True
    assert checkpoint["cursor"] is None
    assert checkpoint["scan_state"] is None
    assert subject.metrics().queued == 1


def test_idle_accounts_do_not_receive_background_history_requests() -> None:
    async def scenario() -> None:
        instance = account("idle")
        service = FakeService({instance.id: instance})
        runtime = FakeRuntime()
        subject = scheduler(service, runtime, InMemoryTradeVolumeLedger())

        subject.bootstrap()
        assert await subject.run_due() is False
        assert runtime.calls == []

    asyncio.run(scenario())


def test_active_event_requests_are_debounced_per_account() -> None:
    async def scenario() -> None:
        instance = account("running", status=InstanceStatus.RUNNING)
        service = FakeService({instance.id: instance})
        runtime = FakeRuntime()
        subject = scheduler(service, runtime, InMemoryTradeVolumeLedger())

        subject.request(instance.id, ACTIVE_EVENT)
        subject.request(instance.id, ACTIVE_EVENT)
        assert await subject.run_due() is True
        assert await subject.run_due() is False
        assert runtime.calls == [instance.id]

    asyncio.run(scenario())


def test_active_request_becoming_idle_clears_its_pending_sync_projection() -> None:
    async def scenario() -> None:
        instance = account("settled", status=InstanceStatus.RUNNING)
        service = FakeService({instance.id: instance})
        ledger = InMemoryTradeVolumeLedger()
        subject = scheduler(service, FakeRuntime(), ledger)

        subject.request(instance.id, ACTIVE_EVENT)
        service.instances[instance.id] = instance.model_copy(update={"status": InstanceStatus.STOPPED})

        assert await subject.run_due() is False
        checkpoint = ledger.sync_checkpoint(instance.id, "live") or {}
        assert checkpoint["pending"] is False
        assert checkpoint["next_sync_at_ms"] is None
        assert checkpoint["sync_reason"] is None

    asyncio.run(scenario())


def test_history_requests_share_one_global_and_proxy_serialization_lane() -> None:
    async def scenario() -> None:
        first = account("one")
        second = account("two", proxy="other:8080")
        service = FakeService({first.id: first, second.id: second})
        runtime = FakeRuntime()
        subject = scheduler(service, runtime, InMemoryTradeVolumeLedger())

        await asyncio.gather(subject.refresh_now(first.id), subject.refresh_now(second.id))

        assert sorted(runtime.calls) == ["one", "two"]
        assert runtime.max_active == 1

    asyncio.run(scenario())


def test_failed_initial_baseline_becomes_pending_without_requeueing() -> None:
    async def scenario() -> None:
        instance = account("initial-failure")
        service = FakeService({instance.id: instance})
        ledger = InMemoryTradeVolumeLedger()
        subject = scheduler(service, FakeRuntime(fail=True), ledger)

        subject.queue_initial_baseline(instance)
        assert await subject.run_due() is True
        assert await subject.run_due() is False
        checkpoint = ledger.sync_checkpoint(instance.id, "live") or {}
        assert checkpoint["initial_baseline_state"] == "pending"
        assert checkpoint["pending"] is False
        assert checkpoint["stale"] is True

    asyncio.run(scenario())


def test_final_session_sync_runs_once_after_instance_becomes_idle() -> None:
    async def scenario() -> None:
        instance = account("final")
        service = FakeService({instance.id: instance})
        runtime = FakeRuntime()
        subject = scheduler(service, runtime, InMemoryTradeVolumeLedger())

        subject.request(instance.id, FINAL_SESSION)
        subject.request(instance.id, FINAL_SESSION)
        assert await subject.run_due() is True
        assert await subject.run_due() is False
        assert runtime.calls == [instance.id]

    asyncio.run(scenario())


def test_final_session_sync_replaces_a_stale_active_event_for_an_idle_account() -> None:
    async def scenario() -> None:
        instance = account("final-over-active")
        service = FakeService({instance.id: instance})
        runtime = FakeRuntime()
        subject = scheduler(service, runtime, InMemoryTradeVolumeLedger())

        subject.request(instance.id, ACTIVE_EVENT)
        subject.request(instance.id, FINAL_SESSION)

        assert await subject.run_due() is True
        assert runtime.calls == [instance.id]

    asyncio.run(scenario())

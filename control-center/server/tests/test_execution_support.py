from decimal import Decimal

from fleet_api.execution import (
    AllocationUnavailable,
    CycleExecutionStatus,
    InMemoryExecutionJournal,
    MockPairedExecutionAdapter,
    PairAllocation,
    PairedCycleCoordinator,
    PairExecutionOutcome,
)
from fleet_api.models import AccountInstance, InstanceStatus, ProxySnapshot, ProxyType, TradingMode
from fleet_api.telemetry import AccountTelemetryContext
from fleet_api.volume_history import InMemoryTradeVolumeLedger


def running_account(instance_id: str = "ins-execution") -> AccountInstance:
    return AccountInstance(
        id=instance_id,
        name="Execution account",
        account_tag="execution",
        api_key_tail="ABCD",
        mode=TradingMode.DEMO,
        status=InstanceStatus.RUNNING,
        phase="运行中",
        proxy=ProxySnapshot(type=ProxyType.HTTPS, host="proxy.example.com:9000"),
    )


class ControlledAdapter:
    def __init__(self, behavior: str) -> None:
        self.behavior = behavior
        self.calls = 0
        self.open_calls = 0
        self.close_calls = 0
        self.cancel_calls = 0
        self.mock = MockPairedExecutionAdapter()

    async def open_once(self, context, plan):
        self.calls += 1
        self.open_calls += 1
        if self.behavior == "raise":
            raise ConnectionError("secret transport detail must not be journaled")
        if self.behavior == "reject":
            return PairExecutionOutcome(CycleExecutionStatus.REJECTED, "post_only_rejected")
        return await self.mock.open_once(context, plan)

    async def close_once(self, context, plan):
        self.calls += 1
        self.close_calls += 1
        if self.behavior == "close_raise":
            raise ConnectionError("secret close transport detail must not be journaled")
        return await self.mock.close_once(context, plan)

    async def cancel_active_orders(self, context):
        self.cancel_calls += 1
        return await self.mock.cancel_active_orders(context)

    async def aclose(self) -> None:
        return None


class SingleAdapterFactory:
    def __init__(self, adapter: ControlledAdapter) -> None:
        self.adapter = adapter

    def create(self, instance_id: str) -> ControlledAdapter:
        return self.adapter


class CountingAllocationProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self, context: AccountTelemetryContext):
        del context
        self.calls += 1
        return PairAllocation(Decimal("0.8"), Decimal("0.2"), "test-allocation-v1")

    async def aclose(self) -> None:
        return None


class UnavailableAllocationProvider(CountingAllocationProvider):
    async def get(self, context: AccountTelemetryContext):
        del context
        self.calls += 1
        raise AllocationUnavailable("beta_unusable")


def coordinator(behavior: str = "complete"):
    journal = InMemoryExecutionJournal()
    ledger = InMemoryTradeVolumeLedger()
    adapter = ControlledAdapter(behavior)
    allocation = CountingAllocationProvider()
    target = PairedCycleCoordinator(
        journal,
        ledger,
        allocation,
        SingleAdapterFactory(adapter),
        total_quote=Decimal("20"),
    )
    return target, journal, ledger, adapter, allocation

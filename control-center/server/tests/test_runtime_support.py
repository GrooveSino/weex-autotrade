import asyncio
from decimal import Decimal

from fleet_api.execution import (
    AllocationUnavailable,
    CancelOrdersOutcome,
    CycleExecutionStatus,
    InMemoryExecutionJournal,
    MockPairedExecutionAdapter,
    PairAllocation,
    PairCyclePlan,
)
from fleet_api.models import (
    ExposureSnapshot,
    ProxyStatus,
    StrategyStage,
    VolumeSnapshot,
    WalletSnapshot,
)
from fleet_api.service import FleetControlService
from fleet_api.telemetry import AccountTelemetry, AccountTelemetryContext


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

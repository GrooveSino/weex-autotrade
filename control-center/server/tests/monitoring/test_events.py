import asyncio
import json
import time
from decimal import Decimal

from fastapi.testclient import TestClient

from fleet_api.config.config import ControlPlaneSettings
from fleet_api.execution import PairAllocation
from fleet_api.main import create_app
from fleet_api.models import (
    AccountInstance,
    InstanceStatus,
    ProxySnapshot,
    ProxyType,
    SchedulerMetrics,
    TradingMode,
)
from fleet_api.monitoring.events import InstanceEventBroker, StrategyMonitorEventBroker


def public_instance(*, name: str = "Event 01") -> AccountInstance:
    return AccountInstance(
        id="ins-event-01",
        name=name,
        account_tag="events",
        api_key_tail="ABCD",
        mode=TradingMode.DEMO,
        status=InstanceStatus.RUNNING,
        phase="Mock 周期运行中",
        proxy=ProxySnapshot(type=ProxyType.HTTPS, host="proxy.test:443"),
    )


class AvailableAllocationProvider:
    async def get(self, context) -> PairAllocation:
        del context
        return PairAllocation(Decimal("0.8"), Decimal("0.2"), "test-allocation-v1")

    async def aclose(self) -> None:
        return None


def test_event_broker_coalesces_slow_subscribers_to_latest_public_snapshot() -> None:
    async def scenario() -> None:
        broker = InstanceEventBroker("generation-test")
        queue = await broker.subscribe()
        await broker.publish([public_instance(name="Old")])
        await broker.publish(
            [public_instance(name="Latest")],
            SchedulerMetrics(
                max_parallel_polls=8,
                max_observed_parallelism=3,
                poll_rounds=4,
                last_round_account_count=2,
                last_round_succeeded=2,
                last_round_duration_ms=87,
            ),
        )

        payload = json.loads(queue.get_nowait())
        assert payload["type"] == "instances"
        assert payload["instances"][0]["name"] == "Latest"
        assert payload["runtime"]["maxParallelPolls"] == 8
        assert payload["runtime"]["lastRoundDurationMs"] == 87
        assert payload["sequence"] == 2
        assert payload["executorGeneration"] == "generation-test"
        assert "credentials" not in payload["instances"][0]
        assert broker.subscriber_count == 1
        await broker.unsubscribe(queue)
        assert broker.subscriber_count == 0

    asyncio.run(scenario())


def test_strategy_monitor_broker_wakes_only_matching_subscribers() -> None:
    async def scenario() -> None:
        broker = StrategyMonitorEventBroker()
        first = broker.subscribe("ins-first")
        second = broker.subscribe("ins-second")

        broker.publish("ins-first")
        broker.publish("ins-first")

        assert first.qsize() == 1
        assert second.empty()
        assert broker.subscriber_count == 2
        assert await first.get() is None

        broker.unsubscribe(first)
        broker.unsubscribe(second)
        assert broker.subscriber_count == 0

    asyncio.run(scenario())


def test_single_background_ticker_updates_running_instances_without_browser_subscribers() -> None:
    app = create_app(
        ControlPlaneSettings(seed_demo_data=True, mock_tick_interval_seconds=0.25),
        allocation_provider=AvailableAllocationProvider(),
    )
    with TestClient(app) as api:
        before = api.get("/api/v1/instances/ins-api-01").json()["volume"]["lifetime"]
        time.sleep(0.35)
        after = api.get("/api/v1/instances/ins-api-01").json()["volume"]["lifetime"]

    assert after > before
    assert app.state.instance_event_broker.subscriber_count == 0

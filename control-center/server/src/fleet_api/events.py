from __future__ import annotations

import asyncio
import json

from .models import AccountInstance, SchedulerMetrics


class InstanceEventBroker:
    def __init__(self, executor_generation: str | None = None) -> None:
        self._subscribers: dict[asyncio.Queue[str], str | None] = {}
        self._lock = asyncio.Lock()
        self._executor_generation = executor_generation
        self._sequence = 0

    def snapshot_payload(
        self,
        instances: list[AccountInstance],
        runtime: SchedulerMetrics | None = None,
        campaigns: list[dict[str, object]] | None = None,
    ) -> str:
        self._sequence += 1
        payload: dict[str, object] = {
            "type": "instances",
            "instances": [instance.model_dump(mode="json", by_alias=True) for instance in instances],
            "sequence": self._sequence,
        }
        if self._executor_generation is not None:
            payload["executorGeneration"] = self._executor_generation
        if runtime is not None:
            payload["runtime"] = runtime.model_dump(mode="json", by_alias=True)
        if campaigns is not None:
            payload["campaigns"] = campaigns
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def sse_message(payload: str) -> str:
        return f"event: instances\ndata: {payload}\n\n"

    async def subscribe(self, owner_user_id: str | None = None) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        async with self._lock:
            self._subscribers[queue] = owner_user_id
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        async with self._lock:
            self._subscribers.pop(queue, None)

    async def publish(
        self,
        instances: list[AccountInstance],
        runtime: SchedulerMetrics | None = None,
        campaigns: list[dict[str, object]] | None = None,
    ) -> None:
        async with self._lock:
            subscribers = tuple(self._subscribers.items())
        for queue, owner_user_id in subscribers:
            owned = (
                instances
                if owner_user_id is None
                else [instance for instance in instances if instance.owner_user_id == owner_user_id]
            )
            owned_ids = {instance.id for instance in owned}
            owned_campaigns = campaigns
            if campaigns is not None and owner_user_id is not None:
                owned_campaigns = [
                    campaign
                    for campaign in campaigns
                    if str(campaign.get("instanceId", campaign.get("instance_id", ""))) in owned_ids
                ]
            payload = self.snapshot_payload(owned, runtime, owned_campaigns)
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(payload)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


class StrategyMonitorEventBroker:
    """Coalesced in-process wakeups for durable strategy-monitor events."""

    def __init__(self) -> None:
        self._subscribers: dict[asyncio.Queue[None], str] = {}

    def subscribe(self, instance_id: str) -> asyncio.Queue[None]:
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._subscribers[queue] = instance_id
        return queue

    def unsubscribe(self, queue: asyncio.Queue[None]) -> None:
        self._subscribers.pop(queue, None)

    def publish(self, instance_id: str) -> None:
        for queue, subscribed_instance_id in tuple(self._subscribers.items()):
            if subscribed_instance_id != instance_id:
                continue
            if queue.full():
                continue
            queue.put_nowait(None)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

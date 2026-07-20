from __future__ import annotations

import asyncio
import json

from .models import AccountInstance, SchedulerMetrics


class InstanceEventBroker:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._lock = asyncio.Lock()

    @staticmethod
    def snapshot_payload(
        instances: list[AccountInstance],
        runtime: SchedulerMetrics | None = None,
        campaigns: list[dict[str, object]] | None = None,
    ) -> str:
        payload: dict[str, object] = {
            "type": "instances",
            "instances": [instance.model_dump(mode="json", by_alias=True) for instance in instances],
        }
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

    async def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    async def publish(
        self,
        instances: list[AccountInstance],
        runtime: SchedulerMetrics | None = None,
        campaigns: list[dict[str, object]] | None = None,
    ) -> None:
        payload = self.snapshot_payload(instances, runtime, campaigns)
        async with self._lock:
            subscribers = tuple(self._subscribers)
        for queue in subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(payload)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

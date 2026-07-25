from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import suppress

from fastapi import FastAPI, Query, Request
from fastapi.responses import StreamingResponse

from fleet_api.bootstrap.main_context import FleetAppContext
from fleet_api.bootstrap.main_helpers import monitor_sse as _monitor_sse


def register_strategy_monitor_routes(app: FastAPI, ctx: FleetAppContext) -> None:
    service = ctx.service
    campaign_journal = ctx.campaign_journal
    strategy_monitor = ctx.strategy_monitor
    monitor_event_broker = ctx.strategy_monitor_event_broker
    executor_generation = ctx.executor_generation

    @app.get("/api/v1/instances/{instance_id}/strategy-monitor/events")
    async def strategy_monitor_events(
        instance_id: str,
        request: Request,
        session_id: str | None = Query(default=None, alias="sessionId", max_length=128),
        after: str | None = Query(default=None, max_length=256),
    ) -> StreamingResponse:
        instance = service.get_instance(instance_id)
        owner_user_id = instance.owner_user_id
        requested_cursor = request.headers.get("last-event-id") or after

        async def stream() -> AsyncIterator[str]:
            strategy_monitor.subscriber_opened()
            wake_queue = monitor_event_broker.subscribe(instance_id)
            try:
                heartbeat_at = time.monotonic()
                initial = await asyncio.to_thread(
                    strategy_monitor.snapshot,
                    instance_id,
                    session_id=session_id,
                    limit=200,
                    owner_user_id=owner_user_id,
                )
                parsed = strategy_monitor.parse_cursor(requested_cursor)
                event_type = "snapshot"
                if parsed is not None:
                    generation, campaign_id, sequence = parsed
                    if (
                        generation != executor_generation
                        or campaign_id != initial.execution_id
                        or sequence > initial.projection_sequence
                    ):
                        event_type = "reset"
                        strategy_monitor.reset_recorded()
                last_campaign_id = initial.execution_id
                last_sequence = initial.projection_sequence
                yield _monitor_sse(
                    event_type,
                    initial.cursor,
                    {
                        "type": event_type,
                        "snapshot": initial,
                        "fromSequence": last_sequence,
                        "toSequence": last_sequence,
                    },
                )

                while not await request.is_disconnected():
                    with suppress(TimeoutError):
                        await asyncio.wait_for(wake_queue.get(), timeout=5)
                    record = await asyncio.to_thread(campaign_journal.monitor_record, instance_id, session_id)
                    campaign_id = record.campaign_id if record is not None else None
                    if campaign_id != last_campaign_id:
                        reset = await asyncio.to_thread(
                            strategy_monitor.snapshot,
                            instance_id,
                            session_id=session_id,
                            limit=200,
                            owner_user_id=owner_user_id,
                        )
                        last_campaign_id = reset.execution_id
                        last_sequence = reset.projection_sequence
                        strategy_monitor.reset_recorded()
                        yield _monitor_sse(
                            "reset",
                            reset.cursor,
                            {"type": "reset", "snapshot": reset, "toSequence": last_sequence},
                        )
                        heartbeat_at = time.monotonic()
                        continue
                    journal_sequence = 0
                    projection_sequence = 0
                    if campaign_id is not None:
                        projection, _unused, journal_sequence = await asyncio.to_thread(
                            campaign_journal.monitor_read,
                            campaign_id,
                            None,
                            1,
                        )
                        projection_sequence = projection.projected_sequence if projection is not None else 0
                        if journal_sequence != projection_sequence or journal_sequence - last_sequence > 200:
                            reset = await asyncio.to_thread(
                                strategy_monitor.snapshot,
                                instance_id,
                                session_id=session_id,
                                limit=200,
                                owner_user_id=owner_user_id,
                            )
                            last_sequence = reset.projection_sequence
                            strategy_monitor.reset_recorded()
                            yield _monitor_sse(
                                "reset",
                                reset.cursor,
                                {"type": "reset", "snapshot": reset, "toSequence": last_sequence},
                            )
                            heartbeat_at = time.monotonic()
                            continue
                        if journal_sequence > last_sequence:
                            rows = await asyncio.to_thread(
                                campaign_journal.events_after,
                                campaign_id,
                                last_sequence,
                                200,
                            )
                            if (
                                not rows
                                or int(rows[0].get("sequence") or 0) != last_sequence + 1
                                or int(rows[-1].get("sequence") or 0) != journal_sequence
                            ):
                                reset = await asyncio.to_thread(
                                    strategy_monitor.snapshot,
                                    instance_id,
                                    session_id=session_id,
                                    limit=200,
                                    owner_user_id=owner_user_id,
                                )
                                last_sequence = reset.projection_sequence
                                strategy_monitor.reset_recorded()
                                yield _monitor_sse(
                                    "reset",
                                    reset.cursor,
                                    {"type": "reset", "snapshot": reset, "toSequence": last_sequence},
                                )
                                heartbeat_at = time.monotonic()
                                continue
                            from_sequence = last_sequence + 1
                            last_sequence = journal_sequence
                            delta = await asyncio.to_thread(
                                strategy_monitor.snapshot,
                                instance_id,
                                session_id=session_id,
                                limit=200,
                                event_rows=rows,
                                owner_user_id=owner_user_id,
                            )
                            delta_cursor = strategy_monitor.cursor(campaign_id, last_sequence)
                            yield _monitor_sse(
                                "delta",
                                delta_cursor,
                                {
                                    "type": "delta",
                                    "snapshot": delta,
                                    "fromSequence": from_sequence,
                                    "toSequence": last_sequence,
                                },
                            )
                            heartbeat_at = time.monotonic()
                            continue
                    if time.monotonic() - heartbeat_at >= 5:
                        now_ms = time.time_ns() // 1_000_000
                        cursor = (
                            strategy_monitor.cursor(campaign_id, last_sequence)
                            if campaign_id and last_sequence
                            else None
                        )
                        yield _monitor_sse(
                            "heartbeat",
                            cursor,
                            {
                                "type": "heartbeat",
                                "journalSequence": journal_sequence,
                                "projectionSequence": projection_sequence,
                                "serverTimeMs": now_ms,
                            },
                        )
                        heartbeat_at = time.monotonic()
            finally:
                monitor_event_broker.unsubscribe(wake_queue)
                strategy_monitor.subscriber_closed()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

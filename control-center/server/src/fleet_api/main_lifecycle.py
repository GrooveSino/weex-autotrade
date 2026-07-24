"""Polling, beta refresh, and shutdown lifecycle for the Fleet ASGI app."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from .main_context import FleetAppContext


def install_application_lifecycle(ctx: FleetAppContext) -> None:
    async def publish_snapshot() -> None:
        await ctx.broker.publish(
            ctx.projected_instances(),
            ctx.runtime.metrics(),
            ctx.campaign_manager.public_snapshot(),
        )

    async def poll_loop() -> None:
        while True:
            await asyncio.sleep(ctx.selected.poll_interval_seconds)
            active_ids = [
                instance.id
                for instance in ctx.service.list_instances()
                if ctx.trade_history_scheduler.is_active(instance)
            ]
            if await ctx.runtime.poll_instances(active_ids):
                await publish_snapshot()

    async def history_sync_loop() -> None:
        while True:
            worked = await ctx.trade_history_scheduler.run_due()
            if worked:
                await publish_snapshot()
            await asyncio.sleep(0.25 if worked else 1)

    async def connection_collect_loop() -> None:
        while True:
            await asyncio.sleep(1)
            await asyncio.to_thread(ctx.campaign_manager.collect_connections)

    async def recovery_loop() -> None:
        while True:
            for record in ctx.strategy_run_lifecycle.due_recovery_records():
                ctx.schedule_session_finalization(record)
            await asyncio.sleep(1)

    async def refresh_beta_state() -> bool:
        available = await ctx.beta_source_runtime.refresh()
        changed = 0
        if ctx.selected_allocation_provider is ctx.beta_source_runtime:
            changed = await ctx.runtime.reconcile_beta_availability(
                available,
                ctx.beta_source_runtime.last_refresh_error,
            )
        if changed:
            await publish_snapshot()
        return available

    async def beta_refresh_loop() -> None:
        while True:
            interval = ctx.beta_source_runtime.settings.refresh_interval_seconds
            await asyncio.sleep(ctx.beta_source_runtime.seconds_until_refresh(interval))
            await refresh_beta_state()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        ctx.event_loop = asyncio.get_running_loop()
        for record in ctx.campaign_journal.list_all():
            if record.metadata.get("strategy_id") and record.status in {
                "completed",
                "stopped",
                "recovering",
                "uncertain",
            }:
                ctx.schedule_session_finalization(record)
        beta_task: asyncio.Task[None] | None = None
        if ctx.selected.beta_background_refresh_enabled:
            await refresh_beta_state()
            beta_task = asyncio.create_task(beta_refresh_loop(), name="fleet-beta-refresher")
        poll_task = asyncio.create_task(poll_loop(), name="fleet-account-poller")
        history_task = asyncio.create_task(history_sync_loop(), name="fleet-history-sync")
        connection_collect_task = asyncio.create_task(connection_collect_loop(), name="fleet-connection-collector")
        recovery_task = asyncio.create_task(recovery_loop(), name="fleet-recovery-supervisor")
        ctx.trade_history_scheduler.bootstrap()
        try:
            yield
        finally:
            poll_task.cancel()
            history_task.cancel()
            connection_collect_task.cancel()
            recovery_task.cancel()
            if beta_task is not None:
                beta_task.cancel()
            with suppress(asyncio.CancelledError):
                await poll_task
            with suppress(asyncio.CancelledError):
                await history_task
            with suppress(asyncio.CancelledError):
                await connection_collect_task
            with suppress(asyncio.CancelledError):
                await recovery_task
            if beta_task is not None:
                with suppress(asyncio.CancelledError):
                    await beta_task
            await ctx.runtime.close()
            await asyncio.to_thread(ctx.campaign_manager.close)
            while ctx.session_finalization_tasks:
                await asyncio.gather(*tuple(ctx.session_finalization_tasks), return_exceptions=True)
            ctx.event_loop = None
            await ctx.beta_source_runtime.aclose()
            ctx.beta_source_store.close()
            ctx.execution_journal.close()
            ctx.volume_ledger.close()
            ctx.vault.close()
            ctx.repository.close()
            ctx.command_ledger.close()

    ctx.publish_snapshot = publish_snapshot
    ctx.refresh_beta_state = refresh_beta_state
    ctx.lifespan = lifespan

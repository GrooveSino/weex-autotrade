"""Install Campaign callbacks that delegate to the lifecycle service."""

from __future__ import annotations

import asyncio

from .main_context import FleetAppContext


def install_campaign_lifecycle(ctx: FleetAppContext) -> None:
    def latest_bound_record(instance_id: str):
        lifecycle = getattr(ctx, "strategy_run_lifecycle", None)
        if lifecycle is not None:
            return lifecycle.latest_bound_record(instance_id)
        records = [
            item
            for item in ctx.campaign_journal.list_for_instance(instance_id)
            if item.metadata.get("execution_kind") == "bound_strategy"
        ]
        return max(records, key=lambda item: item.campaign.created_at_ms) if records else None

    async def finalize_bound_strategy_session(record) -> None:
        session_id = record.metadata.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        try:
            await ctx.strategy_run_lifecycle.finalize_record(record)
        finally:
            latest = ctx.campaign_journal.get(record.campaign_id) or record
            if latest.status in {"completed", "stopped"}:
                ctx.trade_history_scheduler.request(record.instance_id, "final_session")
            ctx.session_finalizations.discard(session_id)
            await ctx.publish_snapshot()

    def schedule_session_finalization(record) -> None:
        session_id = record.metadata.get("session_id")
        if not isinstance(session_id, str) or not session_id or session_id in ctx.session_finalizations:
            return
        ctx.session_finalizations.add(session_id)
        task = asyncio.create_task(
            finalize_bound_strategy_session(record),
            name=f"fleet-session-finalize-{record.campaign_id}",
        )
        ctx.session_finalization_tasks.add(task)
        task.add_done_callback(ctx.session_finalization_tasks.discard)

    def notify_campaign_change(instance_id: str) -> None:
        if ctx.event_loop is None or not ctx.event_loop.is_running():
            return

        def schedule() -> None:
            record = latest_bound_record(instance_id)
            ctx.trade_history_scheduler.request(instance_id, "active_event")
            if record is not None and record.status in {"completed", "stopped", "recovering", "uncertain"}:
                schedule_session_finalization(record)
            asyncio.create_task(ctx.publish_snapshot())

        ctx.event_loop.call_soon_threadsafe(schedule)

    def notify_strategy_monitor_event(instance_id: str, _event) -> None:
        """Wake monitor subscribers only after the journal event has committed."""
        if ctx.event_loop is None or not ctx.event_loop.is_running():
            return
        ctx.event_loop.call_soon_threadsafe(ctx.strategy_monitor_event_broker.publish, instance_id)

    def establish_bound_strategy_session(record, started_at_ms: int) -> None:
        ctx.strategy_run_lifecycle.establish_session(record, started_at_ms)

    ctx.latest_bound_record = latest_bound_record
    ctx.finalize_bound_strategy_session = finalize_bound_strategy_session
    ctx.schedule_session_finalization = schedule_session_finalization
    ctx.notify_campaign_change = notify_campaign_change
    ctx.notify_strategy_monitor_event = notify_strategy_monitor_event
    ctx.establish_bound_strategy_session = establish_bound_strategy_session

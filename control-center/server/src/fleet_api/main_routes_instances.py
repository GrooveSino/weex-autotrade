from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from uuid import uuid4
from fastapi import Query, Request, Response, status
from fastapi.responses import StreamingResponse
from .instance_projection import project_instance_session
from .models import AccountInstance, ExecutionCycleView, GlobalStopRequest, GlobalStopResult, InstanceAction, LogBatch, LogLine, UpdateInstanceRequest, VolumeSessionCreateRequest, VolumeSessionResponse
from .ownership import reset_current_owner_user_id, set_current_owner_user_id
from .service import InstanceNotFound, UnsafeOperation

from fastapi import FastAPI
from .main_context import FleetAppContext
from .main_helpers import execution_cycle_view as _execution_cycle_view


def register_instance_routes(app: FastAPI, ctx: FleetAppContext) -> None:
    selected = ctx.selected
    service = ctx.service
    repository = ctx.repository
    vault = ctx.vault
    volume_ledger = ctx.volume_ledger
    execution_journal = ctx.execution_journal
    execution_coordinator = ctx.execution_coordinator
    selected_allocation_provider = ctx.selected_allocation_provider
    runtime = ctx.runtime
    beta_source_runtime = ctx.beta_source_runtime
    campaign_journal = ctx.campaign_journal
    campaign_manager = ctx.campaign_manager
    app_state_campaign_manager = ctx.campaign_manager
    broker = ctx.broker
    session_volume = ctx.session_volume
    strategy_monitor = ctx.strategy_monitor
    command_ledger = ctx.command_ledger
    executor_generation = ctx.executor_generation
    executor_release_id = ctx.executor_release_id
    latest_bound_record = ctx.latest_bound_record
    finalize_bound_strategy_session = ctx.finalize_bound_strategy_session
    schedule_session_finalization = ctx.schedule_session_finalization
    notify_campaign_change = ctx.notify_campaign_change
    establish_bound_strategy_session = ctx.establish_bound_strategy_session
    publish_snapshot = ctx.publish_snapshot
    refresh_beta_state = ctx.refresh_beta_state
    projected_instances = ctx.projected_instances
    combined_log_updates = ctx.combined_log_updates
    strategy_run_plan = ctx.strategy_run_plan
    require_command_id = ctx.require_command_id

    @app.patch("/api/v1/instances/{instance_id}", response_model=AccountInstance)
    async def update_instance(instance_id: str, payload: UpdateInstanceRequest) -> AccountInstance:
        previous = service.get_instance(instance_id)
        updated = service.update_instance(instance_id, payload)
        await runtime.reset_instance(instance_id)
        history_start_changed = (
            "history_start_at_ms" in payload.model_fields_set
            and payload.history_start_at_ms != previous.history_start_at_ms
        )
        if selected.adapter == "weex-readonly" and (payload.credentials is not None or history_start_changed):
            volume_ledger.remove(instance_id)
            reason = "credentials changed" if payload.credentials is not None else "history start changed"
            updated = service.reset_telemetry_snapshot(instance_id, reason)
        await publish_snapshot()
        return updated

    @app.post("/api/v1/instances/{instance_id}/actions/{action}", response_model=AccountInstance)
    async def apply_action(instance_id: str, action: InstanceAction) -> AccountInstance:
        pending_plan = strategy_run_plan(service.get_instance(instance_id)) if action is InstanceAction.START else None
        try:
            updated = await runtime.apply_action(instance_id, action)
            active_session = volume_ledger.active_session(updated.id, updated.mode.value)
            if action is InstanceAction.START and pending_plan is not None:
                session_volume.start(
                    session_id=f"session-{uuid4().hex}",
                    account_id=updated.id,
                    mode=updated.mode.value,
                    started_at_ms=time.time_ns() // 1_000_000,
                    target_quote_volume=pending_plan.execution_target_quote_volume,
                    maker_only_required=True,
                    strategy_id=updated.strategy.id,
                    strategy_name=updated.strategy.name,
                    strategy_version=updated.strategy.version,
                    target_mode=pending_plan.target_mode.value,
                    strategy_target_quote_volume=pending_plan.strategy_target_quote_volume,
                    baseline_lifetime_quote_volume=pending_plan.baseline_lifetime_quote_volume,
                )
            elif action in {InstanceAction.PAUSE, InstanceAction.STOP} and active_session is not None:
                aggregate = volume_ledger.aggregate(updated.id, 0)
                session_volume.finalize(
                    str(active_session["session_id"]),
                    result="stopped",
                    reason=f"manual_{action.value}",
                    finished_at_ms=time.time_ns() // 1_000_000,
                    final_lifetime_quote_volume=aggregate.lifetime,
                )
            return project_instance_session(updated, volume_ledger, strategy_monitor)
        finally:
            await publish_snapshot()

    @app.post("/api/v1/instances/{instance_id}/positions/close", response_model=AccountInstance)
    async def close_positions(instance_id: str) -> AccountInstance:
        if campaign_journal.active_for_instance(instance_id) is not None:
            raise UnsafeOperation("cannot close positions while a Beta Campaign is active")
        try:
            return await runtime.close_positions(instance_id)
        finally:
            await publish_snapshot()

    @app.post(
        "/api/v1/instances/{instance_id}/volume-sessions",
        response_model=VolumeSessionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_volume_session(instance_id: str, payload: VolumeSessionCreateRequest) -> VolumeSessionResponse:
        instance = service.get_instance(instance_id)
        started_at_ms = payload.started_at_ms or (time.time_ns() // 1_000_000)
        projection = session_volume.start(
            session_id=payload.session_id,
            account_id=instance.id,
            mode=instance.mode.value,
            started_at_ms=started_at_ms,
            target_quote_volume=payload.target_quote_volume,
            maker_only_required=payload.maker_only_required,
        )
        await publish_snapshot()
        return VolumeSessionResponse.model_validate(projection)

    def owned_volume_session(session_id: str):
        session = volume_ledger.get_session(session_id)
        if session is None:
            raise InstanceNotFound("volume session not found")
        # The account lookup is also the ownership authorization check.
        service.get_instance(session.account_id)
        return session

    @app.get("/api/v1/volume-sessions/{session_id}", response_model=VolumeSessionResponse)
    def get_volume_session(session_id: str) -> VolumeSessionResponse:
        owned_volume_session(session_id)
        return VolumeSessionResponse.model_validate(session_volume.progress(session_id))

    @app.get("/api/v1/volume-sessions/{session_id}/fills", response_model=list[dict[str, object]])
    def get_volume_session_fills(session_id: str) -> list[dict[str, object]]:
        session = owned_volume_session(session_id)
        return [
            {
                "fill_id": fill.identity,
                "order_id": fill.order_id,
                "symbol": fill.symbol,
                "executed_at_ms": fill.executed_at_ms,
                "quote_volume": str(fill.quote_volume),
                "base_quantity": str(fill.base_quantity),
                "side": fill.side,
                "position_side": fill.position_side,
                "position_action": fill.position_action,
                "maker": fill.maker,
                "commission": str(fill.commission),
                "commission_asset": fill.commission_asset,
                "realized_pnl": str(fill.realized_pnl),
                "source": fill.source,
                "authoritative": fill.authoritative,
                "created_at_ms": fill.created_at_ms,
            }
            for fill in volume_ledger.fills_for_account(session.account_id, session.mode, session.started_at_ms)
        ]

    @app.post("/api/v1/volume-sessions/{session_id}/sync", response_model=VolumeSessionResponse)
    async def sync_volume_session(session_id: str) -> VolumeSessionResponse:
        session = owned_volume_session(session_id)
        await runtime.refresh_instance(session.account_id)
        checkpoint = volume_ledger.sync_checkpoint(session.account_id, session.mode) or {}
        volume_ledger.update_session(
            session_id,
            cursor=checkpoint.get("cursor"),
            high_watermark_ms=checkpoint.get("high_watermark_ms"),
        )
        projection = session_volume.progress(session_id)
        await publish_snapshot()
        return VolumeSessionResponse.model_validate(projection)

    @app.get("/api/v1/instances/{instance_id}/volume-history", response_model=dict[str, object])
    def account_volume_history(instance_id: str) -> dict[str, object]:
        instance = service.get_instance(instance_id)
        return volume_ledger.account_summary(instance.id, instance.mode.value)

    @app.post("/api/v1/instances/{instance_id}/refresh", response_model=AccountInstance)
    async def refresh_instance(instance_id: str) -> AccountInstance:
        updated = await runtime.refresh_instance(instance_id)
        await publish_snapshot()
        return updated

    @app.get("/api/v1/instances/{instance_id}/logs", response_model=list[LogLine])
    def instance_logs(instance_id: str, limit: int = Query(default=200, ge=1, le=500)) -> list[LogLine]:
        return combined_log_updates(instance_id, limit, None).lines

    @app.get("/api/v1/instances/{instance_id}/log-updates", response_model=LogBatch)
    def instance_log_updates(
        instance_id: str,
        limit: int = Query(default=200, ge=1, le=500),
        after: str | None = Query(default=None, min_length=1, max_length=128),
    ) -> LogBatch:
        return combined_log_updates(instance_id, limit, after)

    @app.delete("/api/v1/instances/{instance_id}/log-updates", status_code=status.HTTP_204_NO_CONTENT)
    def clear_instance_logs(instance_id: str) -> Response:
        service.clear_logs(instance_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/v1/instances/{instance_id}/executions", response_model=list[ExecutionCycleView])
    def instance_executions(
        instance_id: str,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[ExecutionCycleView]:
        service.get_instance(instance_id)
        return [_execution_cycle_view(record) for record in execution_journal.list_recent(instance_id, limit)]

    @app.delete("/api/v1/instances/{instance_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_instance(instance_id: str) -> Response:
        if campaign_journal.active_for_instance(instance_id) is not None:
            raise UnsafeOperation("stop the active Beta Campaign before deleting the account")
        service.delete_instance(instance_id)
        await runtime.remove_instance(instance_id)
        volume_ledger.remove(instance_id)
        campaign_journal.remove(instance_id)
        await publish_snapshot()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/v1/actions/stop-all", response_model=GlobalStopResult)
    async def stop_all(payload: GlobalStopRequest) -> GlobalStopResult:
        result = await runtime.stop_all(payload.confirmation)
        await publish_snapshot()
        return result

    @app.get("/api/v1/events")
    async def instance_events(request: Request) -> StreamingResponse:
        owner_user_id = request.headers.get("X-Fleet-User", "").strip() or None
        queue = await broker.subscribe(owner_user_id)

        async def stream() -> AsyncIterator[str]:
            try:
                with_owner = set_current_owner_user_id(owner_user_id)
                try:
                    initial_instances = projected_instances()
                finally:
                    reset_current_owner_user_id(with_owner)
                owned_ids = {instance.id for instance in initial_instances}
                campaigns = [
                    item
                    for item in campaign_manager.public_snapshot()
                    if owner_user_id is None
                    or str(item.get("instanceId", item.get("instance_id", ""))) in owned_ids
                ]
                yield broker.sse_message(broker.snapshot_payload(initial_instances, runtime.metrics(), campaigns))
                while not await request.is_disconnected():
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                    else:
                        yield broker.sse_message(payload)
            finally:
                await broker.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

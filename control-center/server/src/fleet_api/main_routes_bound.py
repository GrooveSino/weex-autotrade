from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import FastAPI, Query

from .main_context import FleetAppContext
from .models import (
    BetaCampaignEvent,
    BetaCampaignPreview,
    BetaCampaignView,
    BoundStrategyExecutionExecuteRequest,
    BoundStrategyExecutionPreviewRequest,
    BoundStrategyExecutionStopRequest,
    StrategyMonitorSnapshot,
    StrategyRunCapacity,
    StrategyRunCleanupRequest,
    StrategyRunConfirmRequest,
    StrategyRunConfirmResponse,
    StrategyRunPage,
    StrategyRunPhaseQueue,
    StrategyRunPrepareResponse,
    StrategyRunSummary,
    TradingMode,
)
from .service import BetaSourceUnavailable, InstanceNotFound, UnsafeOperation


def _strategy_run_capacity(snapshot) -> StrategyRunCapacity:  # type: ignore[no-untyped-def]
    return StrategyRunCapacity(
        active_executions=snapshot.active_executions,
        max_active_executions=snapshot.max_active_executions,
        active_normal_phases=snapshot.active_normal_phases,
        max_normal_phases=snapshot.max_normal_phases,
        queued_normal_phases=snapshot.queued_normal_phases,
        revision=snapshot.revision,
    )


def register_bound_strategy_routes(app: FastAPI, ctx: FleetAppContext) -> None:
    service = ctx.service
    vault = ctx.vault
    volume_ledger = ctx.volume_ledger
    campaign_manager = ctx.campaign_manager
    strategy_monitor = ctx.strategy_monitor
    publish_snapshot = ctx.publish_snapshot
    strategy_run_plan = ctx.strategy_run_plan
    strategy_run_lifecycle = ctx.strategy_run_lifecycle

    async def prepare_strategy_run(instance_id: str, direction) -> StrategyRunPrepareResponse:
        instance = service.get_instance(instance_id)
        if instance.mode is not TradingMode.LIVE:
            raise UnsafeOperation("bound strategy execution requires a Live account")
        prepared = await strategy_run_lifecycle.prepare(instance, vault.get(instance_id), direction)
        if prepared.disposition != "idle":
            if prepared.disposition == "ready" and prepared.execution is not None:
                preview = BetaCampaignPreview.model_validate(
                    {
                        **prepared.execution.model_dump(),
                        "warnings": ["所有订单固定为 POST_ONLY", "本次完成量仅以已核验成交账本为准"],
                        "blockers": [],
                    }
                )
                return StrategyRunPrepareResponse(disposition="ready", preview=preview)
            return StrategyRunPrepareResponse(
                disposition=prepared.disposition,
                current=prepared.execution,
                reason_code=prepared.reason_code,
                message=prepared.message,
                position_count=prepared.position_count,
                regular_order_count=prepared.regular_order_count,
                trigger_order_count=prepared.trigger_order_count,
                cleanup_confirmation=prepared.cleanup_confirmation,
                blocking_positions=list(prepared.blocking_positions),
                allowed_actions=list(prepared.allowed_actions),
                boundary_checked_at_ms=prepared.boundary_checked_at_ms,
            )
        instance = service.get_instance(instance_id)
        plan = strategy_run_plan(instance, direction)
        session_id = f"session-{uuid4().hex}"
        try:
            view = await asyncio.to_thread(
                strategy_run_lifecycle.create_run_preview,
                instance,
                plan,
                vault.get(instance_id),
                session_id=session_id,
                boundary=dict(prepared.boundary) if prepared.boundary is not None else None,
            )
        except BetaSourceUnavailable as exc:
            return StrategyRunPrepareResponse(
                disposition="unavailable",
                reason_code="final_beta_unavailable",
                message=str(exc),
            )
        preview = BetaCampaignPreview.model_validate(
            {
                **view.model_dump(),
                "warnings": ["所有订单固定为 POST_ONLY", "本次完成量仅以已核验成交账本为准"],
                "blockers": [],
            }
        )
        return StrategyRunPrepareResponse(disposition="ready", preview=preview)

    @app.post(
        "/api/v1/instances/{instance_id}/strategy-run/prepare",
        response_model=StrategyRunPrepareResponse,
    )
    async def prepare_bound_strategy_run(
        instance_id: str,
        payload: BoundStrategyExecutionPreviewRequest,
    ) -> StrategyRunPrepareResponse:
        return await prepare_strategy_run(instance_id, payload.direction)

    @app.post(
        "/api/v1/instances/{instance_id}/strategy-executions/preview",
        response_model=BetaCampaignPreview,
    )
    async def preview_bound_strategy_execution(
        instance_id: str,
        payload: BoundStrategyExecutionPreviewRequest,
    ) -> BetaCampaignPreview:
        prepared = await prepare_strategy_run(instance_id, payload.direction)
        if prepared.disposition != "ready" or prepared.preview is None:
            if prepared.reason_code == "final_beta_unavailable":
                raise BetaSourceUnavailable(prepared.message or "Final Beta source unavailable")
            raise UnsafeOperation(prepared.message or f"strategy run is {prepared.disposition}")
        return prepared.preview

    @app.post(
        "/api/v1/instances/{instance_id}/strategy-run/cleanup",
        response_model=StrategyRunPrepareResponse,
    )
    async def cleanup_bound_strategy_run(
        instance_id: str,
        payload: StrategyRunCleanupRequest,
    ) -> StrategyRunPrepareResponse:
        instance = service.get_instance(instance_id)
        await asyncio.to_thread(
            strategy_run_lifecycle.cleanup_run,
            instance,
            payload.confirmation,
            vault.get(instance_id),
        )
        await publish_snapshot()
        return await prepare_strategy_run(instance_id, payload.direction)

    @app.post(
        "/api/v1/instances/{instance_id}/strategy-run/confirm",
        response_model=StrategyRunConfirmResponse,
    )
    async def confirm_strategy_run(
        instance_id: str,
        payload: StrategyRunConfirmRequest,
    ) -> StrategyRunConfirmResponse:
        instance = service.get_instance(instance_id)
        try:
            execution = await asyncio.to_thread(
                strategy_run_lifecycle.start_run,
                instance,
                payload.execution_id,
                payload.confirmation,
                payload.risk_acknowledged,
                vault.get(instance_id),
            )
        except UnsafeOperation as exc:
            if "execution capacity is full" not in str(exc):
                raise
            capacity = campaign_manager.capacity_snapshot()
            return StrategyRunConfirmResponse(
                admission_state="capacity_full",
                execution_id=payload.execution_id,
                execution=campaign_manager.get(instance_id, payload.execution_id),
                capacity=_strategy_run_capacity(capacity),
            )
        finally:
            await publish_snapshot()
        capacity = campaign_manager.capacity_snapshot()
        actor = campaign_manager.actor_state(payload.execution_id)
        phase_queue = None
        if actor is not None and actor.phase_queue is not None:
            queue = actor.phase_queue
            phase_queue = StrategyRunPhaseQueue(
                position=queue.queue_position,
                estimated_start_at_ms=queue.estimated_start_at_ms,
                proxy_limited=queue.constraint in {"proxy_active", "proxy_cooldown"},
            )
        return StrategyRunConfirmResponse(
            admission_state="admitted",
            execution_id=execution.campaign_id,
            execution=execution,
            capacity=_strategy_run_capacity(capacity),
            phase_queue=phase_queue,
        )

    @app.post(
        "/api/v1/instances/{instance_id}/strategy-executions/{execution_id}/execute",
        response_model=BetaCampaignView,
    )
    async def execute_bound_strategy_execution(
        instance_id: str,
        execution_id: str,
        payload: BoundStrategyExecutionExecuteRequest,
    ) -> BetaCampaignView:
        instance = service.get_instance(instance_id)
        try:
            return await asyncio.to_thread(
                strategy_run_lifecycle.start_run,
                instance,
                execution_id,
                payload.confirmation,
                payload.risk_acknowledged,
                vault.get(instance_id),
            )
        finally:
            await publish_snapshot()

    @app.post(
        "/api/v1/instances/{instance_id}/strategy-executions/{execution_id}/stop",
        response_model=BetaCampaignView,
    )
    async def stop_bound_strategy_execution(
        instance_id: str,
        execution_id: str,
        payload: BoundStrategyExecutionStopRequest,
    ) -> BetaCampaignView:
        instance = service.get_instance(instance_id)
        try:
            return await asyncio.to_thread(
                strategy_run_lifecycle.stop_run,
                instance,
                execution_id,
                payload.confirmation,
                vault.get(instance_id),
            )
        finally:
            await publish_snapshot()

    @app.get(
        "/api/v1/instances/{instance_id}/strategy-executions",
        response_model=list[BetaCampaignView],
    )
    def list_bound_strategy_executions(instance_id: str) -> list[BetaCampaignView]:
        service.get_instance(instance_id)
        return [view for view in campaign_manager.list(instance_id) if view.strategy_id is not None]

    @app.get(
        "/api/v1/instances/{instance_id}/strategy-runs",
        response_model=StrategyRunPage,
    )
    def list_strategy_runs(
        instance_id: str,
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, max_length=128),
    ) -> StrategyRunPage:
        instance = service.get_instance(instance_id)
        rows, next_cursor = volume_ledger.list_sessions(
            instance.id,
            instance.mode.value,
            limit=limit,
            cursor=cursor,
        )
        items = [
            StrategyRunSummary.model_validate(
                {
                    "session_id": row["session_id"],
                    "strategy_id": row.get("strategy_id"),
                    "strategy_name": row.get("strategy_name"),
                    "strategy_version": row.get("strategy_version"),
                    "direction": row.get("direction", "btc_long_eth_short"),
                    "target_mode": row["target_mode"],
                    "started_at_ms": row["started_at_ms"],
                    "finished_at_ms": row.get("finished_at_ms"),
                    "status": row["status"],
                    "audit_status": row.get("audit_status", "pending"),
                    "result": row.get("result"),
                    "result_reason": row.get("result_reason"),
                    "strategy_target_quote_volume": row["strategy_target_quote_volume"],
                    "execution_target_quote_volume": row["target_quote_volume"],
                    "verified_quote_volume": row["verified_quote_volume"],
                    "remaining_quote_volume": row["remaining_quote_volume"],
                    "baseline_lifetime_quote_volume": row["baseline_lifetime_quote_volume"],
                    "final_lifetime_quote_volume": row.get("final_lifetime_quote_volume"),
                    "starting_available_balance_quote": row.get("starting_available_balance_quote"),
                    "ending_available_balance_quote": row.get("ending_available_balance_quote"),
                    "available_balance_change_quote": row.get("available_balance_change_quote"),
                    "source_complete": row["source_complete"],
                    "stale": row["stale"],
                    "reconciliation_required": row["reconciliation_required"],
                }
            )
            for row in rows
        ]
        return StrategyRunPage(items=items, next_cursor=next_cursor)

    @app.get(
        "/api/v1/instances/{instance_id}/strategy-executions/{execution_id}",
        response_model=BetaCampaignView,
    )
    def get_bound_strategy_execution(instance_id: str, execution_id: str) -> BetaCampaignView:
        view = campaign_manager.get(instance_id, execution_id)
        if view.strategy_id is None:
            raise InstanceNotFound("bound strategy execution not found")
        return view

    @app.get(
        "/api/v1/instances/{instance_id}/strategy-executions/{execution_id}/events",
        response_model=list[BetaCampaignEvent],
    )
    def bound_strategy_execution_events(instance_id: str, execution_id: str) -> list[BetaCampaignEvent]:
        view = campaign_manager.get(instance_id, execution_id)
        if view.strategy_id is None:
            raise InstanceNotFound("bound strategy execution not found")
        return campaign_manager.events(instance_id, execution_id)

    @app.get(
        "/api/v1/instances/{instance_id}/strategy-monitor",
        response_model=StrategyMonitorSnapshot,
    )
    def strategy_monitor_snapshot(
        instance_id: str,
        session_id: str | None = Query(default=None, alias="sessionId", max_length=128),
        before_sequence: int | None = Query(default=None, alias="beforeSequence", ge=1),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> StrategyMonitorSnapshot:
        instance = service.get_instance(instance_id)
        return strategy_monitor.snapshot(
            instance_id,
            session_id=session_id,
            before_sequence=before_sequence,
            limit=limit,
            owner_user_id=instance.owner_user_id,
        )

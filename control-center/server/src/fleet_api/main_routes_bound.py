from __future__ import annotations

import asyncio
from uuid import uuid4
from fastapi import Query
from .models import BetaCampaignEvent, BetaCampaignPreview, BetaCampaignView, BoundStrategyExecutionExecuteRequest, BoundStrategyExecutionPreviewRequest, BoundStrategyExecutionReconcileRequest, BoundStrategyExecutionStopRequest, StrategyMonitorSnapshot, StrategyRunPage, StrategyRunSummary, TradingMode
from .service import InstanceNotFound, UnsafeOperation

from fastapi import FastAPI
from .main_context import FleetAppContext


def register_bound_strategy_routes(app: FastAPI, ctx: FleetAppContext) -> None:
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

    @app.post(
        "/api/v1/instances/{instance_id}/strategy-executions/preview",
        response_model=BetaCampaignPreview,
    )
    async def preview_bound_strategy_execution(
        instance_id: str,
        _payload: BoundStrategyExecutionPreviewRequest,
    ) -> BetaCampaignPreview:
        instance = service.get_instance(instance_id)
        if instance.mode is not TradingMode.LIVE:
            raise UnsafeOperation("bound strategy execution requires a Live account")
        plan = strategy_run_plan(instance)
        session_id = f"session-{uuid4().hex}"
        view = await asyncio.to_thread(
            campaign_manager.preview_bound_strategy,
            instance_id,
            instance.strategy,
            plan.execution_target_quote_volume,
            vault.get(instance_id),
            session_id=session_id,
            target_mode=plan.target_mode.value,
            run_disposition=plan.run_disposition,
            strategy_target_quote=plan.strategy_target_quote_volume,
            baseline_lifetime_quote=plan.baseline_lifetime_quote_volume,
            owner_user_id=instance.owner_user_id,
        )
        return BetaCampaignPreview.model_validate(
            {
                **view.model_dump(),
                "warnings": ["所有订单固定为 POST_ONLY", "本次完成量仅以已核验成交账本为准"],
                "blockers": [],
            }
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
        if instance.mode is not TradingMode.LIVE:
            raise UnsafeOperation("bound strategy execution requires a Live account")
        preview = campaign_manager.get(instance_id, execution_id)
        if preview.strategy_id is None:
            raise UnsafeOperation("execution was not created from this account's bound strategy")
        if preview.strategy_id != instance.strategy_id or preview.strategy_version != instance.strategy.version:
            raise UnsafeOperation("bound strategy changed since preview; create a new preview and confirm again")
        try:
            return await asyncio.to_thread(
                campaign_manager.start,
                instance_id,
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
        preview = campaign_manager.get(instance_id, execution_id)
        if preview.strategy_id is None:
            raise UnsafeOperation("execution was not created from this account's bound strategy")
        try:
            return await asyncio.to_thread(campaign_manager.stop, instance_id, execution_id, payload.confirmation)
        finally:
            await publish_snapshot()

    @app.post(
        "/api/v1/instances/{instance_id}/strategy-executions/{execution_id}/reconcile",
        response_model=BetaCampaignView,
    )
    async def reconcile_bound_strategy_execution(
        instance_id: str,
        execution_id: str,
        payload: BoundStrategyExecutionReconcileRequest,
    ) -> BetaCampaignView:
        instance = service.get_instance(instance_id)
        if instance.mode is not TradingMode.LIVE:
            raise UnsafeOperation("bound strategy reconciliation requires a Live account")
        preview = campaign_manager.get(instance_id, execution_id)
        if preview.strategy_id is None:
            raise UnsafeOperation("execution was not created from this account's bound strategy")
        try:
            return await asyncio.to_thread(
                campaign_manager.reconcile,
                instance_id,
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
                    "target_mode": row["target_mode"],
                    "started_at_ms": row["started_at_ms"],
                    "finished_at_ms": row.get("finished_at_ms"),
                    "status": row["status"],
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

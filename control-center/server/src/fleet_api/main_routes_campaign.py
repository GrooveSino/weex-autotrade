from __future__ import annotations

import asyncio
from .models import BetaCampaignEvent, BetaCampaignExecuteRequest, BetaCampaignPreview, BetaCampaignPreviewRequest, BetaCampaignReconcileRequest, BetaCampaignStopRequest, BetaCampaignView, TradingMode
from .service import UnsafeOperation

from fastapi import FastAPI
from .main_context import FleetAppContext


def register_campaign_routes(app: FastAPI, ctx: FleetAppContext) -> None:
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
        "/api/v1/instances/{instance_id}/beta-campaigns/preview",
        response_model=BetaCampaignPreview,
    )
    async def preview_beta_campaign(instance_id: str, payload: BetaCampaignPreviewRequest) -> BetaCampaignPreview:
        instance = service.get_instance(instance_id)
        if instance.mode is not TradingMode.LIVE:
            raise UnsafeOperation("Beta Campaign requires a Live account")
        view = await asyncio.to_thread(
            campaign_manager.preview,
            instance_id,
            payload,
            vault.get(instance_id),
            owner_user_id=instance.owner_user_id,
        )
        return BetaCampaignPreview.model_validate(
            {
                **view.model_dump(),
                "warnings": ["网页端首版仅允许单账号、单 campaign", "所有订单固定为 POST_ONLY"],
                "blockers": [],
            }
        )

    @app.post(
        "/api/v1/instances/{instance_id}/beta-campaigns/{campaign_id}/execute",
        response_model=BetaCampaignView,
    )
    async def execute_beta_campaign(
        instance_id: str,
        campaign_id: str,
        payload: BetaCampaignExecuteRequest,
    ) -> BetaCampaignView:
        instance = service.get_instance(instance_id)
        if instance.mode is not TradingMode.LIVE:
            raise UnsafeOperation("Beta Campaign requires a Live account")
        return await asyncio.to_thread(
            campaign_manager.start,
            instance_id,
            campaign_id,
            payload.confirmation,
            payload.risk_acknowledged,
            vault.get(instance_id),
        )

    @app.post(
        "/api/v1/instances/{instance_id}/beta-campaigns/{campaign_id}/stop",
        response_model=BetaCampaignView,
    )
    async def stop_beta_campaign(
        instance_id: str,
        campaign_id: str,
        payload: BetaCampaignStopRequest,
    ) -> BetaCampaignView:
        service.get_instance(instance_id)
        return await asyncio.to_thread(campaign_manager.stop, instance_id, campaign_id, payload.confirmation)

    @app.post(
        "/api/v1/instances/{instance_id}/beta-campaigns/{campaign_id}/reconcile",
        response_model=BetaCampaignView,
    )
    async def reconcile_beta_campaign(
        instance_id: str,
        campaign_id: str,
        payload: BetaCampaignReconcileRequest,
    ) -> BetaCampaignView:
        instance = service.get_instance(instance_id)
        if instance.mode is not TradingMode.LIVE:
            raise UnsafeOperation("Beta Campaign reconciliation requires a Live account")
        return await asyncio.to_thread(
            campaign_manager.reconcile,
            instance_id,
            campaign_id,
            payload.confirmation,
            vault.get(instance_id),
        )

    @app.get(
        "/api/v1/instances/{instance_id}/beta-campaigns",
        response_model=list[BetaCampaignView],
    )
    def list_beta_campaigns(instance_id: str) -> list[BetaCampaignView]:
        service.get_instance(instance_id)
        return campaign_manager.list(instance_id)

    @app.get(
        "/api/v1/instances/{instance_id}/beta-campaigns/{campaign_id}",
        response_model=BetaCampaignView,
    )
    def get_beta_campaign(instance_id: str, campaign_id: str) -> BetaCampaignView:
        return campaign_manager.get(instance_id, campaign_id)

    @app.get(
        "/api/v1/instances/{instance_id}/beta-campaigns/{campaign_id}/events",
        response_model=list[BetaCampaignEvent],
    )
    def beta_campaign_events(instance_id: str, campaign_id: str) -> list[BetaCampaignEvent]:
        return campaign_manager.events(instance_id, campaign_id)

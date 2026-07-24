from __future__ import annotations

import asyncio

from fastapi import FastAPI

from .main_context import FleetAppContext
from .models import (
    BetaCampaignEvent,
    BetaCampaignExecuteRequest,
    BetaCampaignPreview,
    BetaCampaignPreviewRequest,
    BetaCampaignStopRequest,
    BetaCampaignView,
    TradingMode,
)
from .service import UnsafeOperation


def register_campaign_routes(app: FastAPI, ctx: FleetAppContext) -> None:
    service = ctx.service
    vault = ctx.vault
    campaign_manager = ctx.campaign_manager

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

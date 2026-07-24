"""Pure planning helpers for Campaign actor phases."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from weex_cli.models import decimal_text

from .campaign_actor_models import Campaign, CampaignActorContext


def new_actor_context(campaign_service: Any, campaign: Campaign) -> CampaignActorContext:
    remaining = campaign.target_turnover_quote
    campaign_service._emit(
        "campaign_child_planning_started",
        campaign_id=campaign.campaign_id,
        run=1,
        remaining_quote=decimal_text(remaining),
    )
    child = campaign_service._create_child(campaign, remaining, 1)
    campaign_service.child_store.create(child)
    campaign_service.child_store.claim_for_execution(child)
    campaign_service._emit(
        "campaign_child_planning_completed",
        campaign_id=campaign.campaign_id,
        run=1,
        child_plan_id=child.plan_id,
    )
    campaign_service._emit(
        "campaign_run_started",
        campaign_id=campaign.campaign_id,
        run=1,
        child_plan_id=child.plan_id,
        remaining_quote=decimal_text(remaining),
    )
    return CampaignActorContext(child=child, run_number=1, execution_started_at_ms=campaign_service.now_ms())


def prepare_cycle_leverage(
    service: Any,
    plan: Any,
    sizing: Mapping[str, Any],
    round_number: int,
) -> tuple[int, dict[str, str]]:
    service._emit(
        "leverage_preparing",
        round=round_number,
        opening_notional_quote=sizing["opening_notional_quote"],
    )
    return service._prepare_cycle_leverage(
        plan,
        Decimal(str(sizing["opening_notional_quote"])),
        round_number,
    )

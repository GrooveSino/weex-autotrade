from __future__ import annotations

from decimal import Decimal
from typing import Any

from weex_cli.control_api.exchange import decimal_text

from fleet_api.campaigns.actors.campaign_actor_models import CampaignActorContext
from fleet_api.strategy.strategy import target_tolerance_quote


def completion_tolerance(plan: Any) -> Decimal:
    """Bound the percentage tolerance by one normal full cycle."""
    return min(
        Decimal(str(plan.target_turnover_quote)),
        Decimal(str(plan.round_turnover_quote)),
        target_tolerance_quote(plan.target_turnover_quote),
    )


def desired_cycle_turnover(context: CampaignActorContext) -> Decimal:
    remaining = context.child.target_turnover_quote - context.child_total_quote
    if remaining <= 0:
        raise RuntimeError("campaign child target is already complete")
    normal_cycle = context.child.round_turnover_quote
    tolerance = completion_tolerance(context.child)
    if remaining < normal_cycle and normal_cycle - remaining <= tolerance:
        return normal_cycle
    return min(normal_cycle, remaining)


def campaign_completion_floor(context: CampaignActorContext) -> Decimal | None:
    target = context.child.target_turnover_quote
    if context.child_total_quote >= target:
        return target
    remaining = target - context.child_total_quote
    tolerance = completion_tolerance(context.child)
    normal_cycle_excess = context.child.round_turnover_quote - remaining
    if context.child_total_quote > 0 and remaining <= tolerance and normal_cycle_excess > tolerance:
        return target - tolerance
    return None


def emit_tolerance_acceptance(service: Any, context: CampaignActorContext, floor: Decimal) -> None:
    target = context.child.target_turnover_quote
    if context.child_total_quote >= target:
        return
    service._emit(
        "target_tolerance_accepted",
        total_quote=decimal_text(context.child_total_quote),
        target_quote=decimal_text(target),
        shortfall_quote=decimal_text(target - context.child_total_quote),
        tolerance_quote=decimal_text(target - floor),
    )

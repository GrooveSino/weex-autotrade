"""Pure planning helpers for Campaign actor phases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Any

from weex_cli.beta_allocation import BetaAllocation
from weex_cli.beta_volume import _size_cycle, inspect_live_account
from weex_cli.errors import SafetyError, ValidationError
from weex_cli.models import decimal_text
from weex_cli.reliability import NETWORK_ERRORS

from fleet_api.campaigns.actors.campaign_actor_models import (
    BOUNDARY_COUNTS,
    Campaign,
    CampaignActorContext,
    CycleCondition,
    CycleConditionError,
)


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


def check_cycle_conditions(service: Any, context: CampaignActorContext) -> tuple[BetaAllocation, Mapping[str, Any]]:
    """Read only the prerequisites for a future flat opening attempt."""
    desired = _remaining_cycle_target(context)
    try:
        allocation = _latest_allocation(service)
        opening_notional = desired / 2
        leverage = Decimal(
            str(context.child.max_auto_leverage if context.child.leverage == "auto" else context.child.leverage)
        )
        required_available = opening_notional / leverage * context.child.margin_buffer
        account = inspect_live_account(
            service.gateway,
            required_available,
            opening_notional=opening_notional,
            leverage=context.child.leverage,
            max_auto_leverage=context.child.max_auto_leverage,
            margin_buffer=context.child.margin_buffer,
        )
    except NETWORK_ERRORS as exc:
        raise CycleConditionError(_condition("account_read_retry", str(exc))) from exc
    if not account["available_sufficient"]:
        raise CycleConditionError(_condition("insufficient_available_margin", ""))
    if any(account.get(key) for key in BOUNDARY_COUNTS):
        raise CycleConditionError(_condition("external_account_boundary", ""))
    if allocation.beta <= 0:
        raise CycleConditionError(_condition("beta_unavailable", ""))
    return allocation, account


def build_cycle_plan(
    service: Any,
    context: CampaignActorContext,
) -> tuple[Any, Mapping[str, Any], Any, Any, Mapping[str, Any], Mapping[str, Any]]:
    """Freeze one attempt's Beta, books, quantities, and account boundary."""
    allocation, account = check_cycle_conditions(service, context)
    desired = _remaining_cycle_target(context)
    attempt = context.attempt_number + 1
    plan = replace(
        context.child,
        plan_id=f"{context.child.plan_id}-a{attempt:04d}",
        created_at_ms=service.now_ms(),
        allocation=allocation,
    )
    try:
        lanes = service._create_lanes(plan)
        btc_plan, eth_plan, sizing = service._read_with_retry(
            lambda: _size_cycle(plan, lanes, desired, market_data=getattr(service, "market_data", None)),
            operation="cycle_sizing",
            retry_event="cycle_sizing_retry",
            round=context.round_number,
            attempt=attempt,
        )
    except NETWORK_ERRORS as exc:
        raise CycleConditionError(_condition("shared_market_unavailable", str(exc))) from exc
    except (SafetyError, ValidationError) as exc:
        raise CycleConditionError(_condition_from_sizing_error(exc)) from exc
    context.attempt_number = attempt
    sizing = {
        **sizing,
        "desired_turnover_quote": decimal_text(desired) or "0",
        "planned_turnover_quote": str(sizing.get("estimated_turnover_quote") or "0"),
        "opening_notional_quote": str(sizing.get("opening_notional_quote") or "0"),
        "attempt_number": str(attempt),
        "beta_version": allocation.version,
        "beta_as_of_ms": str(allocation.as_of_ms),
    }
    return plan, {"allocation": allocation, "account": account}, btc_plan, eth_plan, sizing, lanes


def _remaining_cycle_target(context: CampaignActorContext) -> Decimal:
    remaining = context.child.target_turnover_quote - context.child_total_quote
    desired = min(context.child.round_turnover_quote, remaining)
    if desired <= 0:
        raise RuntimeError("campaign child target is already complete")
    return desired


def _latest_allocation(service: Any) -> BetaAllocation:
    if service.provider is None:
        raise CycleConditionError(_condition("beta_unavailable", ""))
    try:
        return service.provider.get()
    except NETWORK_ERRORS as exc:
        raise CycleConditionError(_condition("beta_unavailable", str(exc))) from exc
    except Exception as exc:  # A malformed public value must never reach sizing.
        raise CycleConditionError(_condition("beta_unavailable", str(exc))) from exc


def _condition_from_sizing_error(error: Exception) -> CycleCondition:
    message = str(error).lower()
    if "order book" in message or "shared" in message or "market" in message:
        return _condition("shared_market_unavailable", "")
    if "minimum" in message or "amount" in message or "precision" in message:
        return _condition("minimum_order_infeasible", "")
    if "max_position" in message:
        return _condition("minimum_order_infeasible", "")
    return _condition("account_read_retry", str(error))


def _condition(code: str, detail: str) -> CycleCondition:
    labels = {
        "beta_unavailable": ("最新 Beta 暂不可用，系统会自动读取并继续", "等待最新 Beta 数据恢复"),
        "shared_market_unavailable": ("共享 BTC/ETH 行情暂不可用，系统会自动恢复后继续", "等待共享行情恢复"),
        "account_read_retry": ("账户条件暂时无法读取，系统会自动重新核验", "等待账户读取恢复"),
        "leverage_configuration_unavailable": ("杠杆配置暂不可用，系统不会下单并会自动重新检查", "等待账户配置恢复"),
        "insufficient_available_margin": (
            "可用保证金暂不足，系统不会下单并会自动重新检查",
            "补足可用保证金后等待自动继续",
        ),
        "minimum_order_infeasible": ("当前价格和最小下单量暂不满足本轮条件，系统会重新计算", "等待最小下单量条件恢复"),
        "external_account_boundary": (
            "检测到账户中存在非本任务的仓位或挂单，系统不会自动处理",
            "清理来源不明仓位或挂单后等待自动继续",
        ),
    }
    message, action = labels.get(code, ("执行条件暂不可用，系统会自动重新检查", "等待系统自动重新检查"))
    return CycleCondition(code=code, detail=message if not detail else message, action=action)


def retry_cycle_condition(
    reason: str | None,
    quote: Decimal,
    flat: bool,
    uncertain: bool,
) -> CycleCondition | None:
    if uncertain or not flat:
        return None
    if reason in {"post_only_rejected", "minimum_quantity_rejected", "amount_precision_rejected", "unknown_liquidity"}:
        return CycleCondition(
            code="maker_attempt_unavailable",
            detail="本轮 Maker 条件暂不可用，系统会使用最新行情重新计算后继续",
            action="等待最新行情后自动重新报价",
        )
    if quote == 0:
        return CycleCondition(
            code="empty_cycle",
            detail="本轮未形成可核验成交，系统会在新的行情快照下继续尝试",
            action="等待下一次受控 Maker 尝试",
        )
    return None

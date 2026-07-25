from decimal import Decimal
from types import SimpleNamespace

import pytest
from weex_cli.beta_allocation import BetaAllocation

from fleet_api.campaigns.actors.campaign_actor_models import CampaignActorContext, CycleConditionError
from fleet_api.campaigns.actors.campaign_actor_planning import build_cycle_plan
from fleet_api.campaigns.actors.targets.target_policy import (
    campaign_completion_floor,
    completion_tolerance,
    desired_cycle_turnover,
)


def _context(*, target: str, total: str, normal_cycle: str) -> CampaignActorContext:
    child = SimpleNamespace(
        target_turnover_quote=Decimal(target),
        round_turnover_quote=Decimal(normal_cycle),
    )
    return CampaignActorContext(  # type: ignore[arg-type]
        child=child,
        run_number=1,
        execution_started_at_ms=1,
        child_total_quote=Decimal(total),
    )


def test_small_final_shortfall_completes_when_a_full_cycle_would_overshoot_too_far() -> None:
    context = _context(target="500", total="493.53206", normal_cycle="164.5")

    assert completion_tolerance(context.child) == Decimal("25.00")
    assert campaign_completion_floor(context) == Decimal("475.00")


def test_large_target_prefers_one_normal_cycle_when_overshoot_stays_within_tolerance() -> None:
    context = _context(target="10000", total="9900", normal_cycle="164.5")

    assert campaign_completion_floor(context) is None
    assert desired_cycle_turnover(context) == Decimal("164.5")


def test_remaining_target_is_used_when_neither_completion_path_is_within_tolerance() -> None:
    context = _context(target="500", total="400", normal_cycle="164.5")

    assert campaign_completion_floor(context) is None
    assert desired_cycle_turnover(context) == Decimal("100")


def test_exchange_invalid_order_during_sizing_becomes_a_retryable_minimum_condition(monkeypatch) -> None:
    class InvalidOrder(Exception):
        pass

    allocation = BetaAllocation(
        beta=Decimal("1"),
        btc_long_weight=Decimal("0.5"),
        eth_short_weight=Decimal("0.5"),
        version="beta-1",
        as_of_ms=1,
        confidence=Decimal("1"),
        confidence_threshold=Decimal("0.5"),
        source="fake",
    )
    context = _context(target="500", total="400", normal_cycle="164.5")
    context.child = SimpleNamespace(
        **vars(context.child),
        plan_id="child",
        leverage=400,
        max_auto_leverage=400,
        margin_buffer=Decimal("1.1"),
    )  # type: ignore[assignment]
    service = SimpleNamespace(
        provider=SimpleNamespace(get=lambda: allocation),
        gateway=object(),
        market_data=object(),
        now_ms=lambda: 1,
        _create_lanes=lambda _plan: {"BTC": object(), "ETH": object()},
        _read_with_retry=lambda call, **_fields: call(),
    )
    monkeypatch.setattr(
        "fleet_api.campaigns.actors.campaign_actor_planning.inspect_live_account",
        lambda *_args, **_kwargs: {
            "available_sufficient": True,
            "active_position_count": 0,
            "regular_order_count": 0,
            "trigger_order_count": 0,
        },
    )
    monkeypatch.setattr(
        "fleet_api.campaigns.actors.campaign_actor_planning.replace",
        lambda child, **fields: SimpleNamespace(**{**vars(child), **fields}),
    )
    monkeypatch.setattr(
        "fleet_api.campaigns.actors.campaign_actor_planning._size_cycle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(InvalidOrder("amount below minimum")),
    )

    with pytest.raises(CycleConditionError) as captured:
        build_cycle_plan(service, context)

    assert captured.value.condition.code == "minimum_order_infeasible"

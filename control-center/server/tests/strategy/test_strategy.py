import random
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fleet_api.execution import PairAllocation
from fleet_api.models import (
    AccountInstance,
    InstanceStatus,
    ProxySnapshot,
    ProxyType,
    StrategyDirection,
    StrategyProgress,
    TradingMode,
    VolumeSnapshot,
    VolumeStrategy,
)
from fleet_api.strategy.strategy import (
    StrategyRunBlocked,
    StrategyTargetReached,
    estimate_rounds,
    plan_strategy_cycle,
    random_seconds,
    resolve_strategy_run_plan,
    target_progress_quote,
    target_tolerance_quote,
)


def strategy(**updates: object) -> VolumeStrategy:
    payload: dict[str, object] = {
        "id": "strategy-test",
        "name": "Target volume",
        "targetVolumeQuote": "20000",
        "roundTurnoverQuoteMin": "960",
        "roundTurnoverQuoteMax": "1600",
        "positionHoldMinSeconds": 5,
        "positionHoldMaxSeconds": 15,
        "roundIntervalMinSeconds": 20,
        "roundIntervalMaxSeconds": 30,
    }
    payload.update(updates)
    return VolumeStrategy.model_validate(payload)


def allocation(beta: str = "0.6") -> PairAllocation:
    ratio = Decimal(beta)
    btc_weight = Decimal(1) / (Decimal(1) + ratio)
    return PairAllocation(btc_weight, Decimal(1) - btc_weight, "beta-test")


def test_normal_cycle_randomizes_total_turnover_then_splits_both_legs_from_beta() -> None:
    result = plan_strategy_cycle(strategy(), Decimal(0), allocation(), random.Random(7))

    assert Decimal("960") <= result.turnover_quote <= Decimal("1600")
    assert result.turnover_quote.as_tuple().exponent >= -2
    assert result.eth_short_quote == result.btc_long_quote * Decimal("0.6")
    assert result.total_open_quote == result.btc_long_quote + result.eth_short_quote
    assert result.turnover_quote == result.total_open_quote * 2
    assert result.sizing_mode == "range_random"


def test_last_round_ignores_minimum_and_hits_remaining_target_exactly() -> None:
    result = plan_strategy_cycle(
        strategy(),
        Decimal("19900"),
        allocation(),
        random.Random(9),
    )

    assert result.btc_long_quote == Decimal("31.25")
    assert result.eth_short_quote == Decimal("18.75")
    assert result.total_open_quote == Decimal("50")
    assert result.turnover_quote == Decimal("100")
    assert result.sizing_mode == "residual_finish"


def test_remaining_target_within_user_range_is_also_sized_exactly() -> None:
    result = plan_strategy_cycle(
        strategy(),
        Decimal("18720"),
        allocation(),
        random.Random(11),
    )

    assert result.btc_long_quote == Decimal("400")
    assert result.eth_short_quote == Decimal("240")
    assert result.turnover_quote == Decimal("1280")
    assert result.sizing_mode == "residual_finish"


def test_round_estimate_uses_total_turnover_range_without_beta_assumptions() -> None:
    estimate = estimate_rounds(strategy(), Decimal("1000"))

    assert estimate.minimum == 12
    assert estimate.maximum == 20


def test_completed_target_never_plans_another_cycle() -> None:
    with pytest.raises(StrategyTargetReached):
        plan_strategy_cycle(strategy(), Decimal("20000"), allocation(), random.Random(1))


def test_strategy_rejects_inverted_amount_and_duration_ranges() -> None:
    with pytest.raises(ValidationError):
        strategy(targetVolumeQuoteMin="20001", targetVolumeQuoteMax="20000")
    with pytest.raises(ValidationError):
        strategy(roundTurnoverQuoteMin="1601")
    with pytest.raises(ValidationError):
        strategy(positionHoldMinSeconds=16)
    with pytest.raises(ValidationError):
        strategy(roundIntervalMinSeconds=31)


def test_random_duration_is_inclusive_and_seedable() -> None:
    rng = random.Random(3)
    values = {random_seconds(5, 7, rng) for _ in range(30)}

    assert values == {5, 6, 7}


def test_target_progress_switches_between_incremental_and_lifetime_volume() -> None:
    incremental = strategy(targetMode="incremental")
    lifetime = strategy(targetMode="lifetime")
    instance = AccountInstance(
        id="ins-target-mode",
        name="Target mode",
        account_tag="test",
        api_key_tail="ABCD",
        mode=TradingMode.DEMO,
        status=InstanceStatus.STOPPED,
        phase="stopped",
        proxy=ProxySnapshot(type=ProxyType.HTTPS, host="proxy.example.com:9000"),
        volume=VolumeSnapshot(lifetime=12500, today=500, complete=True),
        strategy_id=incremental.id,
        strategy=incremental,
        strategy_progress=StrategyProgress(generated_volume_quote=Decimal("800")),
    )

    assert target_progress_quote(instance) == Decimal("800")
    assert target_progress_quote(instance, lifetime) == Decimal("12500")


def test_lifetime_cycle_planning_sizes_the_residual_from_account_cumulative_volume() -> None:
    lifetime = strategy(targetMode="lifetime")
    instance = AccountInstance(
        id="ins-lifetime-planning",
        name="Lifetime planning",
        account_tag="test",
        api_key_tail="ABCD",
        mode=TradingMode.DEMO,
        status=InstanceStatus.RUNNING,
        phase="running",
        proxy=ProxySnapshot(type=ProxyType.HTTPS, host="proxy.example.com:9000"),
        volume=VolumeSnapshot(lifetime=19500, today=250, complete=True),
        strategy_id=lifetime.id,
        strategy=lifetime,
        strategy_progress=StrategyProgress(generated_volume_quote=Decimal("9000")),
    )

    result = plan_strategy_cycle(
        lifetime,
        target_progress_quote(instance),
        allocation(),
        random.Random(4),
    )

    assert result.turnover_quote == Decimal("500")
    assert result.sizing_mode == "residual_finish"


def test_target_tolerance_is_deterministic_and_capped_for_large_targets() -> None:
    assert target_tolerance_quote(Decimal("200")) == Decimal("1")
    assert target_tolerance_quote(Decimal("10000")) == Decimal("25.0000")
    assert target_tolerance_quote(Decimal("50000")) == Decimal("50")


def test_incremental_run_samples_one_cent_target_and_persists_direction_in_plan() -> None:
    selected = strategy(targetVolumeQuoteMin="100.00", targetVolumeQuoteMax="101.00")
    instance = AccountInstance(
        id="sample-incremental",
        name="sample",
        account_tag="test",
        api_key_tail="ABCD",
        mode=TradingMode.LIVE,
        status=InstanceStatus.STOPPED,
        phase="idle",
        proxy=ProxySnapshot(type=ProxyType.HTTPS, host="proxy.example.com:9000"),
        strategy_id=selected.id,
        strategy=selected,
    )

    plan = resolve_strategy_run_plan(
        instance,
        None,
        StrategyDirection.BTC_SHORT_ETH_LONG,
        randbelow=lambda size: size - 1,
    )

    assert plan.direction is StrategyDirection.BTC_SHORT_ETH_LONG
    assert plan.strategy_target_quote_volume == Decimal("101")
    assert plan.execution_target_quote_volume == Decimal("101")


def test_lifetime_run_samples_only_unreached_range_and_blocks_at_maximum() -> None:
    selected = strategy(
        targetMode="lifetime",
        targetVolumeQuoteMin="100.00",
        targetVolumeQuoteMax="105.00",
    )
    instance = AccountInstance(
        id="sample-lifetime",
        name="sample",
        account_tag="test",
        api_key_tail="ABCD",
        mode=TradingMode.LIVE,
        status=InstanceStatus.STOPPED,
        phase="idle",
        proxy=ProxySnapshot(type=ProxyType.HTTPS, host="proxy.example.com:9000"),
        volume=VolumeSnapshot(lifetime=100.25, complete=True),
        strategy_id=selected.id,
        strategy=selected,
    )

    plan = resolve_strategy_run_plan(instance, None, randbelow=lambda _size: 0)
    assert plan.strategy_target_quote_volume == Decimal("100.26")
    assert plan.execution_target_quote_volume == Decimal("0.01")

    reached = instance.model_copy(update={"volume": VolumeSnapshot(lifetime=105, complete=True)})
    with pytest.raises(StrategyTargetReached):
        resolve_strategy_run_plan(reached, None)
    with pytest.raises(StrategyRunBlocked):
        resolve_strategy_run_plan(instance, {"status": "active"})


def test_lifetime_run_freezes_current_verified_ledger_even_while_history_audit_is_pending() -> None:
    selected = strategy(
        targetMode="lifetime",
        targetVolumeQuoteMin="12000.00",
        targetVolumeQuoteMax="19000.00",
    )
    instance = AccountInstance(
        id="pending-history-lifetime",
        name="sample",
        account_tag="test",
        api_key_tail="ABCD",
        mode=TradingMode.LIVE,
        status=InstanceStatus.STOPPED,
        phase="idle",
        proxy=ProxySnapshot(type=ProxyType.HTTPS, host="proxy.example.com:9000"),
        volume=VolumeSnapshot(lifetime=250, complete=False),
        strategy_id=selected.id,
        strategy=selected,
    )

    plan = resolve_strategy_run_plan(instance, None, randbelow=lambda _size: 0)

    assert plan.strategy_target_quote_volume == Decimal("12000")
    assert plan.execution_target_quote_volume == Decimal("11750")
    assert plan.baseline_lifetime_quote_volume == Decimal("250")

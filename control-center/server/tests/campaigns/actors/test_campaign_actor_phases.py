from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from weex_cli.beta_allocation import BetaAllocation, BetaUnavailable
from weex_cli.beta_volume import BetaVolumePlan, PairLegPlan

from fleet_api.campaigns.actors.campaign_actor_models import CampaignActorContext, CampaignPhaseEnvironment, OpenCycle
from fleet_api.campaigns.actors.campaign_actor_phases import CampaignActorPhases
from fleet_api.campaigns.actors.campaign_actor_planning import build_cycle_plan


class _Volume:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict[str, Any]]] = []
        self.lane_plans: list[object] = []
        self.current_plan_id = ""
        self._clock_values = iter((10_000, 22_000))

    def _create_lanes(self, plan: object) -> dict[str, object]:
        self.lane_plans.append(plan)
        return {"BTC": object(), "ETH": object()}

    def _preflight_with_read_retry(self, _plan: object) -> dict[str, object]:
        return {"boundary": "flat"}

    def _read_with_retry(self, callback, **_kwargs):  # type: ignore[no-untyped-def]
        return callback()

    def _prepare_cycle_leverage(self, _plan: object, _notional: Decimal, _round: int) -> tuple[int, dict[str, str]]:
        return 400, {"BTC": "400", "ETH": "400"}

    def _run_pair(self, _pool, _plan, _round, _sequence, _specs, _lanes):  # type: ignore[no-untyped-def]
        return {
            "BTC": ({"symbol": "BTC", "quote_volume": "10"}, None),
            "ETH": ({"symbol": "ETH", "quote_volume": "10"}, None),
        }

    def _emit(self, name: str, **fields: object) -> None:
        self.emitted.append((name, dict(fields)))

    def now_ms(self) -> int:
        return next(self._clock_values)


def _plans() -> tuple[PairLegPlan, PairLegPlan]:
    return (
        PairLegPlan(
            "BTC", "long", "buy", "sell", Decimal("50"), Decimal("100"), Decimal("1"), Decimal("0.1"), "bo", "bc"
        ),
        PairLegPlan(
            "ETH", "short", "sell", "buy", Decimal("50"), Decimal("100"), Decimal("1"), Decimal("0.1"), "eo", "ec"
        ),
    )


def _context() -> CampaignActorContext:
    child = SimpleNamespace(
        plan_id="child-plan",
        round_turnover_quote=Decimal("100"),
        target_turnover_quote=Decimal("200"),
        leverage=400,
        max_empty_rounds=3,
        estimated_rounds=2,
    )
    return CampaignActorContext(child=child, run_number=1, execution_started_at_ms=1_000)


def _campaign() -> object:
    return SimpleNamespace(hold_min_seconds=12, hold_max_seconds=12, round_gap_min_seconds=1, round_gap_max_seconds=1)


def _allocation(version: str, beta: str) -> BetaAllocation:
    return BetaAllocation(
        beta=Decimal(beta),
        btc_long_weight=Decimal("0.5"),
        eth_short_weight=Decimal("0.5"),
        version=version,
        as_of_ms=1_000,
        confidence=Decimal("1"),
        confidence_threshold=Decimal("0.5"),
        source="fake",
    )


def _plan(allocation: BetaAllocation) -> BetaVolumePlan:
    btc, eth = _plans()
    return BetaVolumePlan(
        schema_version=5,
        plan_id="child-plan",
        created_at_ms=1,
        target_turnover_quote=Decimal("200"),
        round_turnover_quote=Decimal("100"),
        opening_budget_quote=Decimal("50"),
        max_position_quote=Decimal("500"),
        timeout_seconds=10,
        recovery_attempts=3,
        max_empty_rounds=3,
        cooldown_seconds=1,
        leverage=400,
        max_auto_leverage=125,
        margin_buffer=Decimal("1.1"),
        margin_mode="cross",
        allocation=allocation,
        btc=btc,
        eth=eth,
        estimated_turnover_quote=Decimal("100"),
        direction="btc_long_eth_short",
    )


def _phases(volume: _Volume, *, stopping: bool = False) -> CampaignActorPhases:
    environment = CampaignPhaseEnvironment(SimpleNamespace(), volume, lambda: None)
    return CampaignActorPhases(lambda _phase: environment, is_stopping=lambda: stopping)


def test_initial_beta_outage_enters_the_condition_loop_instead_of_aborting(monkeypatch) -> None:
    claimed: list[object] = []
    service = SimpleNamespace(
        current_campaign=None,
        _validate_authorization=lambda _campaign: (_ for _ in ()).throw(BetaUnavailable("temporary")),
        campaign_store=SimpleNamespace(claim_for_execution=claimed.append),
    )
    environment = CampaignPhaseEnvironment(service, SimpleNamespace(), lambda: None)
    monkeypatch.setattr("fleet_api.campaigns.actors.campaign_actor_phases.new_actor_context", lambda *_args: _context())

    context = CampaignActorPhases(lambda _phase: environment, is_stopping=lambda: False).prepare(_campaign())

    assert context.round_number == 1
    assert claimed == [_campaign()]


def test_hold_timer_starts_only_after_both_open_positions_are_verified(monkeypatch) -> None:
    volume = _Volume()
    btc, eth = _plans()
    monkeypatch.setattr(
        "fleet_api.campaigns.actors.campaign_actor_phases.build_cycle_plan",
        lambda _service, context: (
            context.child,
            {"boundary": "flat"},
            btc,
            eth,
            {"opening_notional_quote": "100", "planned_turnover_quote": "200", "desired_turnover_quote": "200"},
            {"BTC": object(), "ETH": object()},
        ),
    )
    monkeypatch.setattr(
        "fleet_api.campaigns.actors.campaign_actor_phases.observe_positions",
        lambda *_args, **_kwargs: {"BTC": Decimal("1"), "ETH": Decimal("-1")},
    )

    opened = _phases(volume).open(_campaign(), _context())

    assert opened.hold_seconds == 12
    assert opened.started_at_ms == 10_000
    assert opened.hold_started_at_ms == 22_000
    assert [name for name, _ in volume.emitted][-2:] == ["open_barrier_verified", "hold_started"]


def test_open_barrier_does_not_start_hold_until_both_legs_reach_target(monkeypatch) -> None:
    volume = _Volume()
    btc, eth = _plans()
    monkeypatch.setattr(
        "fleet_api.campaigns.actors.campaign_actor_phases.build_cycle_plan",
        lambda _service, context: (
            context.child,
            {"boundary": "flat"},
            btc,
            eth,
            {"opening_notional_quote": "100", "planned_turnover_quote": "200", "desired_turnover_quote": "200"},
            {"BTC": object(), "ETH": object()},
        ),
    )
    monkeypatch.setattr(
        "fleet_api.campaigns.actors.campaign_actor_phases.observe_positions",
        lambda *_args, **_kwargs: {"BTC": Decimal("1"), "ETH": Decimal("0")},
    )

    opened = _phases(volume).open(_campaign(), _context())

    assert opened.hold_seconds == 0
    assert volume.emitted[-1][0] == "open_barrier_not_ready"
    assert "hold_started" not in [name for name, _ in volume.emitted]


def test_actor_position_observation_normalizes_real_float_values() -> None:
    from fleet_api.campaigns.actors.campaign_actor_cycles import observe_positions, positions_are_flat, targets_reached

    btc, eth = _plans()
    service = SimpleNamespace(
        _observe_position=lambda venue, **_kwargs: venue,
    )
    positions = observe_positions(service, {"BTC": SimpleNamespace(venue=1.0), "ETH": SimpleNamespace(venue=-1.0)}, 1)

    assert positions == {"BTC": Decimal("1.0"), "ETH": Decimal("-1.0")}
    assert targets_reached(positions, btc, eth)
    assert not positions_are_flat(positions, btc, eth)


def test_each_attempt_freezes_the_latest_beta_without_mutating_the_authorized_plan(monkeypatch) -> None:
    allocations = iter((_allocation("beta-1", "1"), _allocation("beta-2", "2")))
    service = SimpleNamespace(
        provider=SimpleNamespace(get=lambda: next(allocations)),
        gateway=object(),
        market_data=object(),
        _create_lanes=lambda _plan: {"BTC": object(), "ETH": object()},
        _read_with_retry=lambda call, **_kwargs: call(),
        now_ms=lambda: 2_000,
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
    btc, eth = _plans()
    monkeypatch.setattr(
        "fleet_api.campaigns.actors.campaign_actor_planning._size_cycle",
        lambda plan, _lanes, _desired, **_kwargs: (
            btc,
            eth,
            {"estimated_turnover_quote": "96", "opening_notional_quote": "48"},
        ),
    )
    context = CampaignActorContext(child=_plan(_allocation("preview", "1")), run_number=1, execution_started_at_ms=1)

    first, *_before = build_cycle_plan(service, context)
    context.child_total_quote = Decimal("96")
    second, *_after = build_cycle_plan(service, context)

    assert first.plan_id.endswith("-a0001")
    assert second.plan_id.endswith("-a0002")
    assert (first.allocation.version, second.allocation.version) == ("beta-1", "beta-2")
    assert context.child.allocation.version == "preview"


def test_actor_position_observation_rejects_non_finite_values() -> None:
    from weex_cli.errors import SafetyError

    from fleet_api.campaigns.actors.campaign_actor_cycles import observe_positions

    service = SimpleNamespace(_observe_position=lambda venue, **_kwargs: venue)
    with pytest.raises(SafetyError, match="position quantity observation is invalid"):
        observe_positions(service, {"BTC": SimpleNamespace(venue=float("nan"))}, 1)


def test_close_stage_rebuilds_lanes_from_the_persisted_child_plan(monkeypatch) -> None:
    volume = _Volume()
    btc, eth = _plans()
    context = _context()
    cycle_plan = SimpleNamespace(plan_id="cycle-plan", target_turnover_quote=Decimal("200"))
    opened = OpenCycle(
        context=context,
        preflight={},
        btc_plan=btc,
        eth_plan=eth,
        sizing={"opening_notional_quote": "100", "planned_turnover_quote": "200"},
        selected_leverage=400,
        leverage_state={},
        open_summaries=[],
        lane_stops={},
        started_at_ms=9_000,
        hold_seconds=0,
        execution_plan=cycle_plan,  # type: ignore[arg-type]
    )
    monkeypatch.setattr("fleet_api.campaigns.actors.campaign_actor_phases.close_lanes", lambda *_args: [])
    monkeypatch.setattr(
        "fleet_api.campaigns.actors.campaign_actor_phases.observe_positions",
        lambda *_args, **_kwargs: {"BTC": Decimal("0"), "ETH": Decimal("0")},
    )
    monkeypatch.setattr("fleet_api.campaigns.actors.campaign_actor_phases.positions_are_flat", lambda *_args: True)
    volume._refresh_pending_accounting = lambda *_args: None  # type: ignore[attr-defined]
    volume._final_acceptance = lambda *_args, **_kwargs: {  # type: ignore[attr-defined]
        "status": "completed",
        "reason": "target_verified_complete",
    }
    context.child_total_quote = Decimal("200")

    outcome = _phases(volume).close(_campaign(), opened)

    assert volume.lane_plans == [cycle_plan]
    assert outcome.child_result == {"status": "completed", "reason": "target_verified_complete"}


def test_partial_completed_attempt_advances_round_before_retrying_with_a_fresh_snapshot(monkeypatch) -> None:
    volume = _Volume()
    context = _context()
    cycle_plan = _plan(_allocation("fresh-beta", "1"))
    btc, eth = _plans()
    opened = OpenCycle(
        context=context,
        preflight={},
        btc_plan=btc,
        eth_plan=eth,
        sizing={"opening_notional_quote": "100", "planned_turnover_quote": "200"},
        selected_leverage=400,
        leverage_state={},
        open_summaries=[{"symbol": "BTC", "quote_volume": "10"}],
        lane_stops={"BTC": ("stopped", "post_only_rejected")},
        started_at_ms=9_000,
        hold_seconds=0,
        execution_plan=cycle_plan,
    )
    monkeypatch.setattr(
        "fleet_api.campaigns.actors.campaign_actor_phases.close_lanes",
        lambda *_args: [{"symbol": "BTC", "quote_volume": "10"}],
    )
    monkeypatch.setattr(
        "fleet_api.campaigns.actors.campaign_actor_phases.observe_positions",
        lambda *_args, **_kwargs: {"BTC": Decimal("0"), "ETH": Decimal("0")},
    )
    monkeypatch.setattr("fleet_api.campaigns.actors.campaign_actor_phases.positions_are_flat", lambda *_args: True)
    monkeypatch.setattr(
        "fleet_api.campaigns.actors.campaign_actor_phases._terminal_reason",
        lambda _stops: "post_only_rejected",
    )
    volume._refresh_pending_accounting = lambda *_args: None  # type: ignore[attr-defined]

    outcome = _phases(volume).close(_campaign(), opened)

    assert outcome.condition is not None
    assert outcome.condition.code == "maker_attempt_unavailable"
    assert context.child_total_quote == Decimal("20")
    assert context.round_number == 2


def test_stop_uses_emergency_safe_stop_instead_of_a_normal_close_phase(monkeypatch) -> None:
    volume = _Volume()
    btc, eth = _plans()
    opened = OpenCycle(_context(), {}, btc, eth, {}, 400, {}, [], {}, 9_000, 0)
    called: list[str] = []
    monkeypatch.setattr(
        "fleet_api.campaigns.actors.campaign_actor_phases.safe_stop",
        lambda *_args: called.append("safe") or {"status": "stopped", "reason": "stop_requested"},
    )

    outcome = _phases(volume, stopping=True).close(_campaign(), opened)

    assert called == ["safe"]
    assert outcome.stopped_reason == "stop_requested"

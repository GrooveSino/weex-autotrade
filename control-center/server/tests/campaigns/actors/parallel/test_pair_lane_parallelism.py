from __future__ import annotations

import threading
from decimal import Decimal
from types import SimpleNamespace

from weex_cli.beta_volume import LiveBetaVolumeService, PairLegPlan

from fleet_api.campaigns.actors.campaign_actor_cycles import close_lanes, observe_positions
from fleet_api.campaigns.actors.phase_helpers.open_pair import open_pair


def _plans() -> tuple[PairLegPlan, PairLegPlan]:
    return (
        PairLegPlan(
            "BTC", "long", "buy", "sell", Decimal("50"), Decimal("100"), Decimal("1"), Decimal("0.1"), "bo", "bc"
        ),
        PairLegPlan(
            "ETH", "short", "sell", "buy", Decimal("50"), Decimal("100"), Decimal("1"), Decimal("0.1"), "eo", "ec"
        ),
    )


class _OpenService:
    PAIR_HEARTBEAT_SECONDS = 0.01
    _run_pair = LiveBetaVolumeService._run_pair

    def __init__(self) -> None:
        self.barrier = threading.Barrier(2)
        self.thread_ids: set[int] = set()

    def _execute_leg(self, _plan, _sequence, spec, _lane, _round, *, respect_stop):  # type: ignore[no-untyped-def]
        assert respect_stop is True
        self.thread_ids.add(threading.get_ident())
        self.barrier.wait(timeout=1)
        return {"symbol": spec.plan.symbol}, None

    def _emit(self, _event: str, **_fields: object) -> None:
        return None


def test_actor_open_submits_btc_and_eth_before_waiting_for_either_leg() -> None:
    btc, eth = _plans()
    service = _OpenService()
    plan = SimpleNamespace(
        plan_id="pair-a0001",
        timeout_seconds=10,
        recovery_attempts=1,
        target_turnover_quote=Decimal("200"),
        leverage=400,
    )

    results = open_pair(service, plan, 1, Decimal("100"), Decimal("100"), btc, eth, {"BTC": object(), "ETH": object()})

    assert tuple(results) == ("BTC", "ETH")
    assert len(service.thread_ids) == 2


class _CloseService:
    def __init__(self) -> None:
        self.observe_barrier = threading.Barrier(2)
        self.flatten_barrier = threading.Barrier(2)
        self.observe_threads: set[int] = set()
        self.flatten_threads: set[int] = set()

    def _observe_position(self, venue, **_fields):  # type: ignore[no-untyped-def]
        self.observe_threads.add(threading.get_ident())
        self.observe_barrier.wait(timeout=1)
        return venue.position

    def _flatten_lane(self, _plan, _round, _offset, leg_plan, _lane, **_fields):  # type: ignore[no-untyped-def]
        self.flatten_threads.add(threading.get_ident())
        self.flatten_barrier.wait(timeout=1)
        return [{"symbol": leg_plan.symbol}], True, None

    def _emit(self, _event: str, **_fields: object) -> None:
        return None


def test_actor_close_observes_and_flattens_btc_and_eth_in_parallel() -> None:
    btc, eth = _plans()
    service = _CloseService()
    opened = SimpleNamespace(
        btc_plan=btc,
        eth_plan=eth,
        context=SimpleNamespace(round_number=1),
        plan=SimpleNamespace(),
        open_summaries=[
            {"symbol": "BTC", "executed_quantity": "1", "position_side": "long"},
            {"symbol": "ETH", "executed_quantity": "1", "position_side": "short"},
        ],
    )
    lanes = {
        "BTC": SimpleNamespace(venue=SimpleNamespace(position=1.0)),
        "ETH": SimpleNamespace(venue=SimpleNamespace(position=-1.0)),
    }

    rows = close_lanes(service, lanes, opened, {})  # type: ignore[arg-type]

    assert {row["symbol"] for row in rows} == {"BTC", "ETH"}
    assert len(service.observe_threads) == 2
    assert len(service.flatten_threads) == 2


def test_shared_position_barrier_observes_both_lanes_concurrently() -> None:
    service = _CloseService()
    lanes = {
        "BTC": SimpleNamespace(venue=SimpleNamespace(position=1.0)),
        "ETH": SimpleNamespace(venue=SimpleNamespace(position=-1.0)),
    }

    positions = observe_positions(service, lanes, 1)  # type: ignore[arg-type]

    assert positions == {"BTC": Decimal("1.0"), "ETH": Decimal("-1.0")}
    assert len(service.observe_threads) == 2

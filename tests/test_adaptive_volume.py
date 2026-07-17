from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from weex_cli.adaptive_executor import VenueOrder
from weex_cli.adaptive_maker import MarketSnapshot
from weex_cli.adaptive_volume import (
    REAL_POLICY,
    AdaptiveMakerVolumeService,
    DemoMakerSoakService,
    MakerFlattenService,
    MakerSoakPlan,
    maker_flatten_confirmation,
    maker_soak_confirmation,
)
from weex_cli.maker_volume import MakerVolumePlan


class ServiceGateway:
    def __init__(self) -> None:
        self.active = []

    def open_orders(self, symbol=None, *, trigger=False, mode="live"):
        return list(self.active)

    def amount_to_precision(self, symbol, amount):
        return Decimal(str(amount)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)

    def amount_step(self, symbol):
        return Decimal("0.0001")


class ImmediateVenue:
    def __init__(self, position: float = 0.0) -> None:
        self._now = 0
        self.position = position
        self.orders = {}
        self.next_id = 1

    @property
    def now_ms(self):
        return self._now

    def snapshot(self):
        return MarketSnapshot(self._now, 100.0, 100.1, 0.01, 0.01, 1, 1, 1, 0.1)

    def position_quantity(self):
        return self.position

    def wait_for_submission_slot(self):
        return None

    def submit_post_only(self, side, quantity, price, client_order_id):
        order_id = str(self.next_id)
        self.next_id += 1
        if side == "buy":
            self.position += quantity
        else:
            self.position = max(0.0, self.position - quantity)
        order = VenueOrder(
            order_id,
            client_order_id,
            side,
            price,
            quantity,
            quantity,
            quantity * price,
            "filled",
            True,
            True,
        )
        self.orders[order_id] = order
        return order

    def fetch_order(self, order_id, client_order_id):
        return self.orders[order_id]

    def cancel_order(self, order_id, client_order_id):
        raise AssertionError("immediate fills should not be canceled")

    def advance(self, milliseconds):
        self._now += milliseconds


def test_real_policy_uses_five_tick_passive_guard() -> None:
    assert REAL_POLICY.passive_guard_ticks == 5
    assert REAL_POLICY.urgent_guard_ticks == 1
    assert REAL_POLICY.stale_ticks == 13
    assert REAL_POLICY.max_passive_guard_ticks == 20
    assert REAL_POLICY.volatility_guard_multiplier == 2


def test_flatten_service_reaches_zero_with_pure_maker_fill() -> None:
    gateway = ServiceGateway()
    venue = ImmediateVenue(0.016)
    service = MakerFlattenService(gateway, venue_factory=lambda gateway, symbol: venue)

    result = service.run(
        symbol="BTC", quantity=Decimal("0.016"), max_position_quote=Decimal("1200"), timeout_seconds=120
    )

    assert result["status"] == "completed"
    assert result["maker_only"] is True
    assert result["final_position"] == 0
    assert (
        maker_flatten_confirmation(
            symbol="BTC", quantity=Decimal("0.016"), max_position_quote=Decimal("1200"), timeout_seconds=120
        )
        == "EXECUTE WEEX DEMO MAKER FLATTEN BTC QUANTITY_0.016 MAX_POSITION_1200 TIMEOUT_120"
    )


def test_adaptive_volume_completes_ten_pure_maker_legs_and_finishes_flat() -> None:
    gateway = ServiceGateway()
    venue = ImmediateVenue()
    service = AdaptiveMakerVolumeService(gateway, venue_factory=lambda gateway, symbol: venue)
    plan = MakerVolumePlan.create(
        symbol="BTC", target_quote="10000", fills=10, max_position_quote="1200", timeout_seconds=120
    )

    result = service.run(plan)

    assert result["status"] == "completed"
    assert Decimal(result["total_quote_volume"]) >= Decimal("10000")
    assert result["leg_count"] == 10
    assert result["cycles_completed"] == 5
    assert result["fill_count"] >= 10
    assert result["maker_only"] is True
    assert result["final_position"] == 0


def test_adaptive_services_stop_before_submission_when_open_order_exists() -> None:
    gateway = ServiceGateway()
    gateway.active.append({"id": "existing"})
    venue = ImmediateVenue()
    plan = MakerVolumePlan.create(
        symbol="BTC", target_quote="10000", fills=10, max_position_quote="1200", timeout_seconds=120
    )

    result = AdaptiveMakerVolumeService(gateway, venue_factory=lambda gateway, symbol: venue).run(plan)
    assert result["status"] == "stopped"
    assert result["reason"] == "starting_open_orders_present"
    assert venue.orders == {}


def test_soak_runs_bounded_flat_to_flat_rounds_with_cooldown() -> None:
    gateway = ServiceGateway()
    volume_plan = MakerVolumePlan.create(
        symbol="BTC",
        target_quote="1000",
        fills=2,
        max_position_quote="600",
        timeout_seconds=120,
        poll_interval_seconds=1,
    )
    plan = MakerSoakPlan(volume_plan, 3)
    sleeps = []

    def factory(current_gateway):
        return AdaptiveMakerVolumeService(current_gateway, venue_factory=lambda gateway, symbol: ImmediateVenue())

    result = DemoMakerSoakService(gateway, volume_service_factory=factory, sleep=sleeps.append).run(plan)

    assert result["status"] == "completed"
    assert result["rounds_completed"] == 3
    assert len(result["rounds"]) == 3
    assert sleeps == [10.1, 10.1]
    assert all(row["final_position"] == 0 for row in result["rounds"])


def test_soak_confirmation_covers_every_bounded_parameter() -> None:
    volume_plan = MakerVolumePlan.create(
        symbol="BTC",
        target_quote="10000",
        fills=10,
        max_position_quote="1200",
        timeout_seconds=120,
        poll_interval_seconds=1,
    )

    phrase = maker_soak_confirmation(MakerSoakPlan(volume_plan, 3))

    assert phrase == ("EXECUTE WEEX DEMO MAKER SOAK BTC TARGET_10000 FILLS_10 ROUNDS_3 MAX_POSITION_1200 TIMEOUT_120")

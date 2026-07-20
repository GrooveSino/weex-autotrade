from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

import ccxt
import pytest

from weex_cli.errors import ValidationError
from weex_cli.maker_volume import MakerVolumePlan, MakerVolumeService, maker_volume_confirmation


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class MakerGateway:
    def __init__(self, outcome: str = "filled") -> None:
        self.outcome = outcome
        self.history: list[dict] = []
        self.position_size = Decimal("0")
        self.intents = []
        self.history_calls = 0
        self.history_failures = 0

    def positions(self, mode, symbol=None):
        if self.position_size == 0:
            return []
        return [{"symbol": "BTCSUSDT", "side": "LONG", "size": str(self.position_size)}]

    def order_book(self, symbol, limit):
        return {"bids": [["100", "10"]], "asks": [["101", "10"]]}

    def amount_to_precision(self, symbol, amount):
        return Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    def place_order(self, intent):
        self.intents.append(intent)
        if self.outcome == "network":
            raise ccxt.RequestTimeout("submit timed out")
        if self.outcome == "rejected":
            return {"success": False, "errorCode": "POST_ONLY", "errorMessage": "maker rejected"}

        status = "FILLED"
        executed = intent.quantity
        if self.outcome == "canceled":
            status = "CANCELED"
            executed = Decimal("0")
        elif self.outcome == "partial":
            status = "CANCELED"
            executed = intent.quantity / 2
        elif self.outcome == "timeout":
            status = "NEW"
            executed = Decimal("0")

        if executed > 0:
            if intent.reduce_only:
                self.position_size -= executed
            else:
                self.position_size += executed
        quote = executed * (intent.price or Decimal("0"))
        self.history.insert(
            0,
            {
                "orderId": str(len(self.intents)),
                "clientOrderId": intent.client_order_id,
                "symbol": "BTCSUSDT",
                "status": status,
                "origQty": str(intent.quantity),
                "executedQty": str(executed),
                "avgPrice": str(intent.price),
                "cumQuote": str(quote),
                "timeInForce": "POST_ONLY",
            },
        )
        return {"success": True, "orderId": str(len(self.intents)), "clientOrderId": intent.client_order_id}

    def order_history(self, mode, symbol=None, limit=100, start_time=None, end_time=None):
        self.history_calls += 1
        if self.history_failures > 0:
            self.history_failures -= 1
            raise ccxt.RequestTimeout("history timeout")
        return self.history[:limit]


def plan(**overrides) -> MakerVolumePlan:
    values = {
        "symbol": "BTC",
        "target_quote": "400",
        "fills": 4,
        "max_position_quote": "120",
        "timeout_seconds": 2,
        "poll_interval_seconds": 0.5,
    }
    values.update(overrides)
    return MakerVolumePlan.create(**values)


def service(gateway: MakerGateway) -> MakerVolumeService:
    clock = FakeClock()
    maker = MakerVolumeService(
        gateway,  # type: ignore[arg-type]
        clock=clock,
        sleep=clock.sleep,
        prefix_factory=lambda symbol: "mv-btc-test",
    )
    maker.trading.sleep = lambda _: None
    return maker


def test_plan_and_confirmation_match_batch_contract() -> None:
    target = plan(target_quote="100000", fills=10, max_position_quote="12000", timeout_seconds=120)
    assert maker_volume_confirmation(target) == (
        "EXECUTE WEEX DEMO MAKER VOLUME BTC TARGET_100000 FILLS_10 MAX_POSITION_12000 TIMEOUT_120"
    )
    assert target.as_dict()["cycles"] == 5


def test_successful_batch_alternates_open_close_and_ends_flat() -> None:
    gateway = MakerGateway()
    result = service(gateway).run(plan())

    assert result["status"] == "completed"
    assert result["fill_count"] == 4
    assert Decimal(result["total_quote_volume"]) >= 400
    assert result["final_position"] == {"active": False, "count": 0, "side": None, "size": "0"}
    assert [intent.side for intent in gateway.intents] == ["buy", "sell", "buy", "sell"]
    assert [intent.reduce_only for intent in gateway.intents] == [False, True, False, True]
    assert all(intent.time_in_force == "POST_ONLY" for intent in gateway.intents)
    assert len({intent.client_order_id for intent in gateway.intents}) == 4


@pytest.mark.parametrize(
    ("outcome", "status", "reason"),
    [
        ("canceled", "stopped", "post_only_canceled"),
        ("partial", "uncertain", "partial_fill_then_canceled"),
        ("timeout", "uncertain", "fill_timeout"),
        ("network", "uncertain", "submission_outcome_uncertain"),
        ("rejected", "stopped", "submission_rejected"),
    ],
)
def test_failed_attempt_never_submits_a_second_order(outcome: str, status: str, reason: str) -> None:
    gateway = MakerGateway(outcome)
    result = service(gateway).run(plan())

    assert result["status"] == status
    assert result["reason"] == reason
    assert result["attempt_count"] == 1
    assert len(gateway.intents) == 1


def test_partial_cancel_reports_remaining_position() -> None:
    gateway = MakerGateway("partial")
    result = service(gateway).run(plan())

    assert result["final_position"]["active"] is True
    assert Decimal(result["final_position"]["size"]) > 0


def test_transient_history_read_errors_recover_without_resubmission() -> None:
    gateway = MakerGateway()
    gateway.history_failures = 2
    result = service(gateway).run(plan())

    assert result["status"] == "completed"
    assert len(gateway.intents) == 4


def test_repeated_history_read_errors_stop_as_uncertain_without_resubmission() -> None:
    gateway = MakerGateway()
    gateway.history_failures = 9
    result = service(gateway).run(plan())

    assert result["status"] == "uncertain"
    assert result["reason"] == "order_history_unavailable"
    assert len(gateway.intents) == 1


def test_existing_position_stops_before_market_or_submission_calls() -> None:
    gateway = MakerGateway()
    gateway.position_size = Decimal("1")
    result = service(gateway).run(plan())

    assert result["status"] == "stopped"
    assert result["reason"] == "starting_position_not_flat"
    assert gateway.intents == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"fills": 3},
        {"fills": 0},
        {"timeout_seconds": 0},
        {"poll_interval_seconds": 0.1},
        {"target_quote": "480", "fills": 4, "max_position_quote": "120"},
    ],
)
def test_invalid_or_infeasible_plans_are_rejected(overrides) -> None:
    with pytest.raises(ValidationError):
        plan(**overrides)

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from fleet_api.campaigns.actors.campaign_actor_models import CampaignActorContext, OpenCycle
from fleet_api.campaigns.actors.closing.close_cycle import close_cycle


def _plans() -> tuple[SimpleNamespace, SimpleNamespace]:
    return (
        SimpleNamespace(position_side="long", opening_side="buy", quantity=Decimal("1"), amount_step=Decimal("0.1")),
        SimpleNamespace(
            position_side="short", opening_side="sell", quantity=Decimal("0.2"), amount_step=Decimal("0.1")
        ),
    )


def _opened() -> OpenCycle:
    child = SimpleNamespace(target_turnover_quote=Decimal("100"), round_turnover_quote=Decimal("20"))
    context = CampaignActorContext(child=child, run_number=1, execution_started_at_ms=1)
    btc, eth = _plans()
    return OpenCycle(
        context=context,
        preflight={},
        btc_plan=btc,
        eth_plan=eth,
        sizing={"opening_notional_quote": "10", "planned_turnover_quote": "20"},
        selected_leverage=400,
        leverage_state={},
        open_summaries=[
            {
                "symbol": "ETH",
                "position_side": "short",
                "action": "open",
                "accounting_verified": True,
                "executed_quantity": "0.2",
                "quote_volume": "10",
            }
        ],
        lane_stops={},
        started_at_ms=1,
        hold_seconds=0,
    )


class _Service:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def _emit(self, name: str, **fields: object) -> None:
        self.events.append((name, fields))

    def _refresh_pending_accounting(self, *_args: object) -> None:
        return

    def now_ms(self) -> int:
        return 10


def test_confirmed_owned_close_rejection_keeps_cycle_for_only_remaining_leg() -> None:
    service, opened = _Service(), _opened()
    campaign = SimpleNamespace(round_gap_min_seconds=0, round_gap_max_seconds=0)

    def rejected(_service, _lanes, _opened, stops):  # type: ignore[no-untyped-def]
        stops["ETH"] = ("stopped", "post_only_rejected")
        return []

    first = close_cycle(
        service,
        {},
        campaign,
        opened,
        close_lanes_fn=rejected,
        observe_positions_fn=lambda *_args: {"BTC": Decimal(0), "ETH": Decimal("-0.2")},
        terminal_reason_fn=lambda stops: stops["ETH"][1],
    )

    assert first.close_condition is not None
    assert opened.close_summaries == []
    assert opened.context.child_total_quote == 0
    assert opened.context.round_number == 1

    def filled(_service, _lanes, _opened, _stops):  # type: ignore[no-untyped-def]
        return [{"symbol": "ETH", "position_side": "short", "action": "close", "quote_volume": "10"}]

    second = close_cycle(
        service,
        {},
        campaign,
        opened,
        close_lanes_fn=filled,
        observe_positions_fn=lambda *_args: {"BTC": Decimal(0), "ETH": Decimal(0)},
        terminal_reason_fn=lambda _stops: None,
    )

    assert second.stopped_reason is None
    assert opened.context.child_total_quote == Decimal("20")
    assert len(opened.close_summaries) == 1
    assert opened.context.round_number == 2

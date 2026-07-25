from decimal import Decimal

from fleet_api.market.funding import funding_preflight
from fleet_api.models import FundingPreflightStatus, VolumeStrategy


def strategy(**updates: object) -> VolumeStrategy:
    payload: dict[str, object] = {
        "id": "strategy-funding",
        "name": "Funding gate",
        "targetVolumeQuote": "20000",
        "roundTurnoverQuoteMin": "800",
        "roundTurnoverQuoteMax": "1000",
    }
    payload.update(updates)
    return VolumeStrategy.model_validate(payload)


def test_funding_preflight_selects_smallest_leverage_with_buffer_below_100x() -> None:
    result = funding_preflight(strategy(), Decimal("10"), wallet_known=True)

    assert result.status is FundingPreflightStatus.READY
    assert result.opening_notional_quote == Decimal("500")
    assert result.required_leverage == 60
    assert result.planned_leverage == 60
    assert result.max_leverage == 99
    assert result.safety_buffer == Decimal("1.20")
    assert result.max_supported_turnover_quote == Decimal("1650")


def test_funding_preflight_blocks_when_required_leverage_exceeds_99x() -> None:
    result = funding_preflight(strategy(), Decimal("5"), wallet_known=True)

    assert result.status is FundingPreflightStatus.INSUFFICIENT
    assert result.required_leverage == 120
    assert result.planned_leverage is None
    assert result.reason == "required_leverage_exceeds_99x"


def test_funding_preflight_remains_pending_until_wallet_is_known() -> None:
    result = funding_preflight(strategy(), Decimal(0), wallet_known=False)

    assert result.status is FundingPreflightStatus.PENDING
    assert result.available_quote is None
    assert result.reason == "wallet_not_synchronized"

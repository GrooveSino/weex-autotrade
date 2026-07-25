from __future__ import annotations

from decimal import ROUND_CEILING, Decimal, localcontext

from fleet_api.models import (
    FundingPreflightSnapshot,
    FundingPreflightStatus,
    VolumeStrategy,
)

MAX_AUTO_LEVERAGE = 99
MARGIN_SAFETY_BUFFER = Decimal("1.20")


def funding_preflight(
    strategy: VolumeStrategy,
    available_quote: Decimal | float | int,
    *,
    wallet_known: bool,
) -> FundingPreflightSnapshot:
    available = Decimal(str(available_quote))
    opening_notional = strategy.round_turnover_quote_max / Decimal(2)
    if not wallet_known:
        return FundingPreflightSnapshot(
            opening_notional_quote=opening_notional,
            reason="wallet_not_synchronized",
        )

    if not available.is_finite() or available <= 0:
        return FundingPreflightSnapshot(
            status=FundingPreflightStatus.INSUFFICIENT,
            available_quote=max(Decimal(0), available) if available.is_finite() else Decimal(0),
            opening_notional_quote=opening_notional,
            max_supported_turnover_quote=Decimal(0),
            reason="available_balance_zero",
        )

    with localcontext() as context:
        context.prec = 50
        buffered_notional = opening_notional * MARGIN_SAFETY_BUFFER
        required_leverage = int((buffered_notional / available).to_integral_value(rounding=ROUND_CEILING))
        required_leverage = max(1, required_leverage)
        max_supported_turnover = available * Decimal(MAX_AUTO_LEVERAGE) / MARGIN_SAFETY_BUFFER * Decimal(2)

    if required_leverage > MAX_AUTO_LEVERAGE:
        return FundingPreflightSnapshot(
            status=FundingPreflightStatus.INSUFFICIENT,
            available_quote=available,
            opening_notional_quote=opening_notional,
            required_leverage=required_leverage,
            max_supported_turnover_quote=max_supported_turnover,
            reason="required_leverage_exceeds_99x",
        )
    return FundingPreflightSnapshot(
        status=FundingPreflightStatus.READY,
        available_quote=available,
        opening_notional_quote=opening_notional,
        required_leverage=required_leverage,
        planned_leverage=required_leverage,
        max_supported_turnover_quote=max_supported_turnover,
        reason="ready",
    )

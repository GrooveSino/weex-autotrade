from __future__ import annotations

from weex_cli.core.config import Settings
from weex_cli.core.errors import SafetyError
from weex_cli.core.models import OrderIntent, decimal_text


def order_confirmation(intent: OrderIntent) -> str:
    price = decimal_text(intent.price) or "MARKET"
    tif = intent.time_in_force or "NONE"
    return " ".join(
        [
            "EXECUTE",
            "WEEX",
            intent.mode.upper(),
            "ORDER",
            intent.exchange_symbol,
            intent.side.upper(),
            intent.position_side.upper(),
            intent.order_type.upper(),
            decimal_text(intent.quantity) or "0",
            price,
            tif,
        ]
    )


def action_confirmation(mode: str, action: str, *parts: object) -> str:
    return " ".join(["EXECUTE", "WEEX", mode.upper(), action.upper(), *(str(part).upper() for part in parts)])


def require_execution(*, execute: bool, supplied: str, expected: str, mode: str, settings: Settings) -> None:
    if not execute:
        raise SafetyError("execution flag is required")
    if supplied.strip() != expected:
        raise SafetyError(f"confirmation mismatch; expected exactly: {expected}")
    if mode == "live" and not settings.live_trading_enabled:
        raise SafetyError("live trading is disabled; set WEEX_LIVE_TRADING_ENABLED=true after reviewing the plan")

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Any

from weex_cli.core.errors import SafetyError, ValidationError
from weex_cli.core.models import decimal_text
from weex_cli.exchange.rest.gateway import WeexGateway, summarize_position_size
from weex_cli.execution.venues import LiveAdaptiveMakerVenue

from .contracts import (
    DEFAULT_STRATEGY_DIRECTION,
    MARGIN_BUFFER,
    MAX_AUTO_LEVERAGE,
    MAX_FIXED_LEVERAGE,
    STRATEGY_DIRECTIONS,
    PairLegPlan,
)
from .numeric import decimal_from_exchange


def select_leverage(
    leverage: str | int,
    opening_notional: Decimal,
    available_quote: Decimal,
    *,
    max_auto_leverage: int = MAX_AUTO_LEVERAGE,
    margin_buffer: Decimal = MARGIN_BUFFER,
) -> int:
    normalized = _normalize_leverage(leverage)
    if not opening_notional.is_finite() or opening_notional <= 0:
        raise ValidationError("opening_notional must be positive and finite")
    if not available_quote.is_finite() or available_quote <= 0:
        raise SafetyError("available USDT is zero or unavailable")
    with localcontext() as context:
        context.prec = 50
        required = int((opening_notional * margin_buffer / available_quote).to_integral_value(rounding=ROUND_CEILING))
    required = max(1, required)
    if normalized == "auto":
        if required > max_auto_leverage:
            raise SafetyError(
                f"this cycle requires {required}x leverage, above the {max_auto_leverage}x automatic limit"
            )
        return required
    fixed = int(normalized)
    if fixed < required:
        raise SafetyError(f"fixed {fixed}x leverage is insufficient; this cycle requires at least {required}x")
    return fixed


def _normalize_leverage(value: object) -> str | int:
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "auto":
            return "auto"
        try:
            parsed = int(text)
        except ValueError:
            raise ValidationError(f"leverage must be 'auto' or an integer between 1 and {MAX_FIXED_LEVERAGE}") from None
    elif isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    else:
        raise ValidationError(f"leverage must be 'auto' or an integer between 1 and {MAX_FIXED_LEVERAGE}")
    if not 1 <= parsed <= MAX_FIXED_LEVERAGE:
        raise ValidationError(f"leverage must be 'auto' or an integer between 1 and {MAX_FIXED_LEVERAGE}")
    return parsed


def _normalize_margin_mode(value: object) -> str:
    normalized = str(value).strip().lower()
    if normalized == "crossed":
        normalized = "cross"
    if normalized not in {"isolated", "cross"}:
        raise ValidationError("margin_mode must be isolated or cross")
    return normalized


def _normalize_direction(value: object) -> str:
    normalized = str(value).strip().lower()
    if normalized not in STRATEGY_DIRECTIONS:
        raise ValidationError("strategy direction is unsupported")
    return normalized


def _direction_sides(direction: str, symbol: str) -> tuple[str, str, str]:
    normal = direction == DEFAULT_STRATEGY_DIRECTION
    is_long = normal if symbol == "BTC" else not normal
    return ("long", "buy", "sell") if is_long else ("short", "sell", "buy")


def signed_open_quantity(leg: PairLegPlan) -> float:
    quantity = float(leg.quantity)
    return quantity if leg.opening_side == "buy" else -quantity


def _available_quote(gateway: WeexGateway) -> Decimal:
    rows = gateway.account_balance_rows("live")
    usdt = next((row for row in rows if str(row.get("asset") or "").upper() == "USDT"), None)
    if usdt is None:
        raise ValidationError("WEEX balance response has no USDT row")
    return decimal_from_exchange(usdt.get("availableBalance"), "available USDT")


def _ensure_lane_leverage(
    gateway: WeexGateway,
    symbol: str,
    position_side: str,
    leverage: int,
    *,
    margin_mode: str = "isolated",
    read_leverage: Callable[[], Mapping[str, Any]] | None = None,
) -> str:
    expected_margin_mode = _normalize_margin_mode(margin_mode)
    observe = read_leverage or (lambda: gateway.leverage(symbol))
    try:
        current = observe()
    except Exception as exc:  # noqa: BLE001 - classified without exposing exchange payloads
        raise SafetyError(f"{symbol.lower()}_leverage_read_{type(exc).__name__.lower()}") from exc
    changes: list[str] = []
    if _observed_margin_mode(current) != expected_margin_mode:
        try:
            gateway.configure_margin_mode(symbol, expected_margin_mode)
        except Exception as exc:  # noqa: BLE001 - mutation may have landed; only observe, never resubmit
            try:
                current = observe()
            except Exception:
                current = {}
            if _observed_margin_mode(current) == expected_margin_mode:
                changes.append("margin_updated_after_uncertain_response")
            else:
                raise SafetyError(f"{symbol.lower()}_margin_mode_update_{type(exc).__name__.lower()}") from exc
        else:
            try:
                current = observe()
            except Exception as exc:  # noqa: BLE001 - a successful mutation still requires proof
                raise SafetyError(f"{symbol.lower()}_margin_mode_verify_{type(exc).__name__.lower()}") from exc
            if _observed_margin_mode(current) != expected_margin_mode:
                raise SafetyError(f"{symbol.lower()}_margin_mode_verify_mismatch")
            changes.append("margin_updated")
    if _leverage_matches(current, position_side, leverage, expected_margin_mode):
        return "+".join(changes) if changes else "unchanged"
    try:
        gateway.configure_leverage(symbol, leverage, expected_margin_mode)
    except Exception as exc:  # noqa: BLE001 - mutation may have landed; only observe, never resubmit
        try:
            observed = observe()
        except Exception:
            observed = {}
        if _leverage_matches(observed, position_side, leverage, expected_margin_mode):
            changes.append("leverage_updated_after_uncertain_response")
            return "+".join(changes)
        raise SafetyError(f"{symbol.lower()}_leverage_update_{type(exc).__name__.lower()}") from exc
    try:
        verified = observe()
    except Exception as exc:  # noqa: BLE001 - a successful mutation still requires proof
        raise SafetyError(f"{symbol.lower()}_leverage_verify_{type(exc).__name__.lower()}") from exc
    if not _leverage_matches(verified, position_side, leverage, expected_margin_mode):
        raise SafetyError(f"{symbol.lower()}_leverage_verify_mismatch")
    changes.append("leverage_updated")
    return "+".join(changes)


def _observed_margin_mode(payload: Mapping[str, Any]) -> str:
    observed = str(payload.get("marginMode") or payload.get("marginType") or "").strip().lower()
    return "cross" if observed in {"cross", "crossed"} else observed


def _leverage_matches(
    payload: Mapping[str, Any],
    position_side: str,
    expected: int,
    margin_mode: str = "isolated",
) -> bool:
    normalized_margin = _normalize_margin_mode(margin_mode)
    if _observed_margin_mode(payload) != normalized_margin:
        return False
    keys = (
        ("crossLeverage", "leverage", "longLeverage" if position_side == "long" else "shortLeverage")
        if normalized_margin == "cross"
        else ("longLeverage" if position_side == "long" else "shortLeverage",)
    )
    raw = next((payload.get(key) for key in keys if payload.get(key) is not None), None)
    try:
        actual = Decimal(str(raw))
    except Exception:  # noqa: BLE001 - malformed exchange observation is simply non-matching
        return False
    return actual == Decimal(expected)


def _cycle_leverage_failure_reason(exc: Exception) -> str:
    if isinstance(exc, SafetyError):
        message = str(exc).lower()
        if message and all(character.isalnum() or character == "_" for character in message):
            return message
        if "available usdt" in message or "requires" in message or "automatic limit" in message:
            return "cycle_funding_insufficient"
        return "cycle_leverage_verification_failed"
    return f"cycle_leverage:{type(exc).__name__.lower()}"


def inspect_live_account(
    gateway: WeexGateway,
    required_available: Decimal,
    *,
    opening_notional: Decimal | None = None,
    leverage: str | int = "auto",
    max_auto_leverage: int = MAX_AUTO_LEVERAGE,
    margin_buffer: Decimal = MARGIN_BUFFER,
) -> dict[str, Any]:
    available = _available_quote(gateway)
    active_positions = 0
    regular_orders = 0
    trigger_orders = 0
    position_sizes: dict[str, str | None] = {}
    blocking_positions: list[dict[str, str]] = []
    for symbol in ("BTC", "ETH"):
        position_rows = gateway.positions("live", symbol)
        sizes = [abs(Decimal(summarize_position_size(row))) for row in position_rows]
        active_positions += sum(1 for size in sizes if size > 0)
        position_sizes[symbol] = decimal_text(sum(sizes, Decimal(0)))
        for row, size in zip(position_rows, sizes, strict=True):
            if size <= 0:
                continue
            info = row.get("info") if isinstance(row.get("info"), Mapping) else {}
            side = str(row.get("side") or info.get("positionSide") or info.get("side") or "unknown").lower()
            notional = _position_notional(row, info, size)
            blocking_positions.append(
                {
                    "symbol": symbol,
                    "side": side if side in {"long", "short"} else "unknown",
                    "quantity": decimal_text(size) or "0",
                    "approximate_quote": decimal_text(notional) or "0",
                }
            )
        regular_orders += len(gateway.open_orders(symbol, mode="live"))
        trigger_orders += _row_count(gateway.algo_orders(symbol))
    result: dict[str, Any] = {
        "funds_configured": True,
        "available_quote": decimal_text(available),
        "available_sufficient": available >= required_available,
        "active_position_count": active_positions,
        "position_sizes": position_sizes,
        "blocking_positions": blocking_positions,
        "regular_order_count": regular_orders,
        "trigger_order_count": trigger_orders,
    }
    if opening_notional is not None and available >= required_available:
        result["planned_leverage"] = select_leverage(
            leverage,
            opening_notional,
            available,
            max_auto_leverage=max_auto_leverage,
            margin_buffer=margin_buffer,
        )
    return result


def _position_notional(row: Mapping[str, Any], info: Mapping[str, Any], quantity: Decimal) -> Decimal:
    for key in ("notional", "openValue", "positionValue"):
        raw = row.get(key) if row.get(key) is not None else info.get(key)
        try:
            value = abs(Decimal(str(raw)))
        except Exception:  # noqa: BLE001 - incomplete public position metadata is tolerated
            continue
        if value.is_finite() and value > 0:
            return value
    for key in ("markPrice", "entryPrice"):
        raw = row.get(key) if row.get(key) is not None else info.get(key)
        try:
            price = abs(Decimal(str(raw)))
        except Exception:  # noqa: BLE001
            continue
        if price.is_finite() and price > 0:
            return quantity * price
    return Decimal(0)


def observed_recovery_quantity(gateway: WeexGateway, symbol: str, position_side: str) -> Decimal:
    rows = gateway.positions("live", symbol)
    quantities = [
        Decimal(summarize_position_size(row))
        for row in rows
        if str(row.get("side") or row.get("positionSide") or "").lower() == position_side.lower()
        and Decimal(summarize_position_size(row)) > 0
    ]
    if len(quantities) > 1:
        raise SafetyError(f"multiple active {symbol} {position_side} positions require manual reconciliation")
    return quantities[0] if quantities else Decimal(0)


def _row_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, Mapping):
        rows = value.get("rows") or value.get("data") or value.get("list") or []
        return len(rows) if isinstance(rows, list) else 0
    return 0


def _safe_position(venue: LiveAdaptiveMakerVenue) -> float | None:
    try:
        return venue.position_quantity()
    except Exception:  # noqa: BLE001 - reporting must preserve the original uncertain state
        return None

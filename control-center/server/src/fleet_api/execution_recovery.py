"""Pure recovery scheduling and execution-ownership classification."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .campaign_contracts import CampaignRecord

RECOVERY_BACKOFF_MS = (1_000, 2_000, 5_000, 10_000, 30_000)


def recovery_delay_ms(attempt: int) -> int:
    return RECOVERY_BACKOFF_MS[min(max(1, attempt), len(RECOVERY_BACKOFF_MS)) - 1]


def recovery_due(record: CampaignRecord, now_ms: int) -> bool:
    if record.status not in {"recovering", "uncertain"}:
        return False
    next_check = _integer(record.metadata.get("next_recovery_check_at_ms"))
    return next_check is None or next_check <= now_ms


def boundary_state(record: CampaignRecord, boundary: dict[str, object]) -> str:
    if bool(boundary.get("flat")):
        return "flat"
    positions = boundary.get("blocking_positions")
    if not isinstance(positions, list) or not positions:
        return "unknown"
    ownership = record.metadata.get("execution_ownership")
    if not isinstance(ownership, dict):
        return "external_exposure"
    legs = ownership.get("legs")
    if not isinstance(legs, dict):
        return "external_exposure"
    for position in positions:
        if not isinstance(position, dict):
            return "external_exposure"
        symbol = str(position.get("symbol") or "").upper()
        leg = legs.get(symbol)
        if not isinstance(leg, dict) or str(position.get("side") or "").lower() != leg.get("position_side"):
            return "external_exposure"
        quantity = _decimal(position.get("quantity"))
        owned = _decimal(leg.get("owned_quantity"))
        tolerance = _decimal(leg.get("amount_step")) / 2
        if quantity <= 0 or owned <= 0 or quantity > owned + tolerance:
            return "external_exposure"
    return "owned_exposure"


def recovery_metadata(
    record: CampaignRecord,
    *,
    now_ms: int,
    state: str,
    boundary: str,
    reason: str | None = None,
) -> dict[str, Any]:
    attempt = max(0, _integer(record.metadata.get("recovery_attempt")) or 0) + 1
    return {
        "recovery_state": state,
        "recovery_attempt": attempt,
        "last_recovery_check_at_ms": now_ms,
        "next_recovery_check_at_ms": now_ms + recovery_delay_ms(attempt),
        "recovery_boundary_state": boundary,
        "recovery_reason": reason,
    }


def _decimal(value: object) -> Decimal:
    try:
        parsed = abs(Decimal(str(value or 0)))
    except Exception:
        return Decimal(0)
    return parsed if parsed.is_finite() else Decimal(0)


def _integer(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None

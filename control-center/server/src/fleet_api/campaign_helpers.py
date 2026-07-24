from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from weex_cli.beta_campaign import BetaVolumeCampaign, campaign_confirmation
from weex_cli.errors import SafetyError
from weex_cli.gateway import WeexGateway

from .campaign_contracts import CampaignRecord
from .models import BetaCampaignStatus
from .service import ValidationFailed

def _preview_metadata(campaign: BetaVolumeCampaign, available: Decimal, readiness: dict[str, Any]) -> dict[str, Any]:
    confirmation = campaign_confirmation(campaign)
    effective_leverage = campaign.leverage if isinstance(campaign.leverage, int) else campaign.max_auto_leverage
    return {
        "confirmation": confirmation,
        "stop_confirmation": f"STOP WEEX LIVE BETA-CAMPAIGN {campaign.campaign_id.upper()} POST_ONLY",
        "available_quote": str(available),
        "required_leverage": effective_leverage,
        "planned_leverage": effective_leverage,
        "max_supported_turnover_quote": str(
            available * Decimal(effective_leverage) / campaign.margin_buffer * Decimal(2)
        ),
        "readiness": readiness,
        "phase": "planned",
    }


def _bound_strategy_confirmation(campaign: BetaVolumeCampaign) -> str:
    return (
        f"EXECUTE WEEX LIVE STRATEGY {campaign.campaign_id.upper()} "
        f"DIRECTION_{campaign.direction.upper()} POST_ONLY"
    )


def _bound_strategy_stop_confirmation(campaign_id: str) -> str:
    return f"STOP WEEX LIVE STRATEGY {campaign_id.upper()} POST_ONLY"


def _available_quote(gateway: WeexGateway) -> Decimal:
    rows = gateway.account_balance_rows("live")
    for row in rows:
        if str(row.get("asset") or "").upper() == "USDT":
            try:
                value = Decimal(str(row.get("availableBalance") or row.get("available") or "0"))
            except Exception as exc:  # noqa: BLE001
                raise ValidationFailed("WEEX available balance is invalid") from exc
            if not value.is_finite() or value < 0:
                raise ValidationFailed("WEEX available balance is invalid")
            return value
    raise ValidationFailed("WEEX account balance has no USDT row")


def _available_quote_from_readiness(readiness: Mapping[str, Any]) -> Decimal:
    """Reuse the validated balance from the just-completed account boundary check."""
    try:
        value = Decimal(str(readiness["available_quote"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise ValidationFailed("WEEX available balance is invalid") from exc
    if not value.is_finite() or value < 0:
        raise ValidationFailed("WEEX available balance is invalid")
    return value


def _normalize_proxy_url(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if "://" in text:
        return text
    return f"https://{text}"


def _reconciliation_confirmation(campaign_id: str) -> str:
    return f"RECONCILE WEEX LIVE BETA-CAMPAIGN {campaign_id.upper()} ACCOUNT_FLAT NO_ORDERS"


def _cleanup_confirmation(campaign_id: str) -> str:
    return f"CLEANUP WEEX LIVE STRATEGY {campaign_id.upper()} CANCEL_AND_MAKER_FLATTEN"


def _reconciliation_required(record: CampaignRecord) -> bool:
    return (
        record.status == BetaCampaignStatus.UNCERTAIN.value
        and record.metadata.get("reconciliation_acknowledged_at_ms") is None
    )


def _account_boundary_is_flat(boundary: dict[str, Any]) -> bool:
    return all(
        int(boundary.get(key, -1)) == 0
        for key in ("active_position_count", "regular_order_count", "trigger_order_count")
    )


def _campaign_result_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """Project authoritative child accounting into the control-plane journal."""
    rows = result.get("children")
    children = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    if not children and isinstance(result.get("accounting"), dict):
        children = [result]
    totals = {
        "fill_count": 0,
        "maker_count": 0,
        "taker_count": 0,
        "unknown_count": 0,
        "order_count": 0,
        "cancel_count": 0,
        "requote_count": 0,
        "btc_quote": Decimal(0),
        "eth_quote": Decimal(0),
        "maker_quote": Decimal(0),
        "taker_quote": Decimal(0),
        "unknown_quote": Decimal(0),
    }
    for child in children:
        accounting = child.get("accounting")
        if isinstance(accounting, dict):
            totals["fill_count"] += _int_field(accounting, "fill_count")
            totals["maker_count"] += _int_field(accounting, "maker_count")
            totals["taker_count"] += _int_field(accounting, "taker_count")
            totals["unknown_count"] += _int_field(accounting, "unknown_liquidity_count")
            quote = _decimal_field(accounting, "executed_quote_volume")
            if bool(accounting.get("maker_only")):
                totals["maker_quote"] += quote
            elif quote:
                totals["unknown_quote"] += quote
        legs = child.get("legs")
        if isinstance(legs, list):
            for leg in legs:
                if not isinstance(leg, dict):
                    continue
                symbol = str(leg.get("symbol") or "").upper()
                quote = _decimal_field(leg, "quote_volume")
                if symbol == "BTC":
                    totals["btc_quote"] += quote
                elif symbol == "ETH":
                    totals["eth_quote"] += quote
                for key, target in (("submissions", "order_count"), ("cancels", "cancel_count")):
                    value = leg.get(key)
                    if isinstance(value, list):
                        totals[target] += len(value)
        timeline = child.get("timeline")
        if isinstance(timeline, list):
            totals["requote_count"] += sum(
                1
                for event in timeline
                if isinstance(event, dict) and "requote" in str(event.get("event") or event.get("name") or "").lower()
            )
    if not children:
        fallback_quote = _decimal_field(result, "executed_quote_volume")
        if bool(result.get("maker_only")):
            totals["maker_quote"] = fallback_quote
        elif fallback_quote:
            totals["unknown_quote"] = fallback_quote
    return {
        "fill_count": totals["fill_count"],
        "maker_count": totals["maker_count"],
        "taker_count": totals["taker_count"],
        "unknown_count": totals["unknown_count"],
        "order_count": totals["order_count"],
        "cancel_count": totals["cancel_count"],
        "requote_count": totals["requote_count"],
        "btc_quote": str(totals["btc_quote"]),
        "eth_quote": str(totals["eth_quote"]),
        "maker_quote": str(totals["maker_quote"]),
        "taker_quote": str(totals["taker_quote"]),
        "unknown_quote": str(totals["unknown_quote"]),
    }


def _int_field(payload: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(payload.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _decimal_field(payload: dict[str, Any], key: str) -> Decimal:
    try:
        value = Decimal(str(payload.get(key) or 0))
    except Exception:  # noqa: BLE001 - malformed result is reported as zero
        return Decimal(0)
    return value if value.is_finite() and value >= 0 else Decimal(0)


def _worker_exception_reason(exc: Exception) -> str:
    """Return an actionable but non-sensitive terminal worker reason.

    Exchange exceptions can embed request URLs, account identifiers, or raw
    responses.  The journal is visible in the control center, so only a small
    vocabulary of known safety conditions is persisted.  Unknown exceptions
    retain their class only and enter read-only recovery after submission.
    """
    if not isinstance(exc, SafetyError):
        return f"worker_exception:{type(exc).__name__.lower()}"
    message = str(exc).lower()
    known_codes = (
        ("available usdt", "available_balance_insufficient"),
        ("positions or orders", "account_boundary_not_flat"),
        ("flat btc/eth positions", "account_boundary_not_flat"),
        ("timing policy", "timing_policy_unavailable"),
        ("beta provider", "beta_source_unavailable"),
        ("beta moved", "beta_changed_since_preview"),
        ("authorization expired", "authorization_expired"),
        ("campaign authorization expired", "authorization_expired"),
        ("leverage", "leverage_verification_failed"),
        ("post_only", "post_only_verification_failed"),
    )
    for token, code in known_codes:
        if token in message:
            return f"worker_safety:{code}"
    return "worker_safety:preflight_rejected"

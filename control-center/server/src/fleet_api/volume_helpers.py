from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from .volume_contracts import (
    SHANGHAI,
    NormalizedTradeFill,
    TradeVolumeAggregate,
    VolumeSession,
)


def utc_day_start_ms(now_ms: int) -> int:
    if now_ms < 0:
        raise ValueError("timestamp cannot be negative")
    instant = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    start = instant.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def shanghai_day_start_ms(now_ms: int) -> int:
    if now_ms < 0:
        raise ValueError("timestamp cannot be negative")
    instant = datetime.fromtimestamp(now_ms / 1000, tz=SHANGHAI)
    start = instant.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def _aggregate(
    fills: tuple[NormalizedTradeFill, ...],
    today_start_ms: int,
    complete: bool,
) -> TradeVolumeAggregate:
    lifetime = sum((fill.quote_volume for fill in fills), start=Decimal(0))
    today = sum(
        (fill.quote_volume for fill in fills if fill.executed_at_ms >= today_start_ms),
        start=Decimal(0),
    )
    return TradeVolumeAggregate(lifetime, today, len(fills), complete)


def _fill_signature(fill: NormalizedTradeFill) -> tuple[object, ...]:
    return (
        fill.identity,
        fill.executed_at_ms,
        fill.quote_volume,
        fill.symbol,
        fill.order_id,
        fill.base_quantity,
        fill.side,
        fill.position_side,
        fill.position_action,
        fill.maker,
        fill.commission,
        fill.commission_asset,
        fill.realized_pnl,
        fill.source,
        fill.authoritative,
    )


def _fill_summary(fills: list[NormalizedTradeFill]) -> dict[str, object]:
    opening = sum((f.quote_volume for f in fills if f.position_action == "open"), Decimal(0))
    closing = sum((f.quote_volume for f in fills if f.position_action == "close"), Decimal(0))
    maker = sum((f.quote_volume for f in fills if f.maker is True), Decimal(0))
    taker = sum((f.quote_volume for f in fills if f.maker is False), Decimal(0))
    unknown = sum((f.quote_volume for f in fills if f.maker is None), Decimal(0))
    return {
        "fill_count": len(fills),
        "total_quote_volume": str(sum((f.quote_volume for f in fills), Decimal(0))),
        "opening_quote_volume": str(opening),
        "closing_quote_volume": str(closing),
        "maker_quote_volume": str(maker),
        "taker_quote_volume": str(taker),
        "unknown_liquidity_quote_volume": str(unknown),
        "authoritative_fill_count": sum(1 for f in fills if f.authoritative),
    }


def _in_session_window(fill: NormalizedTradeFill, session: VolumeSession) -> bool:
    """Keep terminal session accounting isolated from later runs."""
    return fill.executed_at_ms >= session.started_at_ms and (
        session.finished_at_ms is None or fill.executed_at_ms <= session.finished_at_ms
    )


def _session_projection(
    session: VolumeSession,
    fills: list[NormalizedTradeFill],
    verified: Decimal,
) -> dict[str, object]:
    summary = _fill_summary(fills)
    remaining = max(session.target_quote_volume - verified, Decimal(0))
    eligible_maker = all(f.maker is True for f in fills if f.authoritative) if fills else True
    status = _normalized_session_status(session.status)
    available_balance_change = (
        None
        if session.starting_available_balance_quote is None or session.ending_available_balance_quote is None
        else session.ending_available_balance_quote - session.starting_available_balance_quote
    )
    return {
        **session.as_dict(),
        **summary,
        "verified_quote_volume": str(verified),
        "remaining_quote_volume": str(remaining),
        "status": status,
        "audit_status": session.audit_status,
        "maker_only_verified": eligible_maker,
        "available_balance_change_quote": (None if available_balance_change is None else str(available_balance_change)),
        "retry_allowed": False,
    }


def _normalized_session_status(status: str) -> str:
    if status == "running":
        return "active"
    if status == "stale" or status == "verification_pending":
        return "stopped"
    if status == "uncertain" or status.startswith("uncertain:"):
        return "recovering"
    if status == "paused":
        return "stopped"
    return status

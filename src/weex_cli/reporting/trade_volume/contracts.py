"""Public contracts for the local Demo trade-volume cache."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from weex_cli.core.errors import ValidationError

DAY_MS = 24 * 60 * 60 * 1000
DEMO_WINDOW_MS = 90 * DAY_MS
DEMO_PAGE_LIMIT = 1000


class TradeHistoryGateway(Protocol):
    def trade_rows(
        self,
        mode: str,
        symbol: str | None,
        *,
        start_time: int,
        end_time: int,
        limit: int,
        page: int | None = None,
    ) -> list[dict[str, object]]: ...


class TradeVolumeRateLimited(RuntimeError):
    """The source asked the cache synchronizer to slow down."""


@dataclass(frozen=True)
class CachedTrade:
    trade_id: str
    order_id: str
    symbol: str
    timestamp: int
    quote_volume: Decimal
    action: str
    liquidity: str


@dataclass(frozen=True)
class SyncState:
    history_start_ms: int
    backfill_end_ms: int
    cursor_ms: int
    next_page: int
    backfill_complete: bool
    last_poll_ms: int


def account_fingerprint(api_key: str) -> str:
    if not api_key:
        raise ValidationError("An API key is required to isolate the local volume cache")
    return hashlib.sha256(api_key.encode()).hexdigest()[:24]

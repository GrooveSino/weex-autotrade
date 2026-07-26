"""Stable contracts and constants shared by WEEX WebSocket streams."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

WEEX_PUBLIC_WS_URL = "wss://ws-contract.weex.com/v3/ws/public"
WEEX_PRIVATE_WS_URL = "wss://ws-contract.weex.com/v3/ws/private"
WEEX_PRIVATE_WS_PATH = "/v3/ws/private"
DEFAULT_BOOK_MAX_AGE_SECONDS = 3.0


class OrderBookGateway(Protocol):
    def order_book(self, symbol: str, limit: int = 15) -> dict[str, Any]: ...


class MarketStreamUnavailable(RuntimeError):
    """The cached WebSocket book cannot safely serve a quote."""


@dataclass
class DepthState:
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    update_id: int | None = None
    event_time_ms: int | None = None
    received_at: float = 0.0

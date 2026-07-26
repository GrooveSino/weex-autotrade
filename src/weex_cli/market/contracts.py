"""Shared contracts and immutable values for market tick collection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

WEEX_PUBLIC_WS_URL = "wss://ws-contract.weex.com/v3/ws/public"
DEFAULT_WEBSOCKET_STALE_AFTER_SECONDS = 30.0


class MarketGateway(Protocol):
    def ticker(self, symbol: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Tick:
    symbol: str
    price: float


@dataclass(frozen=True)
class CollectionResult:
    captured_at: float
    ticks: tuple[Tick, ...]
    rows_written: int


@dataclass
class CollectorStats:
    cycles: int = 0
    rows_written: int = 0
    rows_deleted: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    ignored_ticks: int = 0
    last_prices: dict[str, float] = field(default_factory=dict)

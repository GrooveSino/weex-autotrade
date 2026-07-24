from __future__ import annotations

from typing import Any, Protocol


class MarketCloseStore(Protocol):
    def claim_market_close_intent(self, plan: Any, key: str, *, created_at_ms: int) -> bool: ...

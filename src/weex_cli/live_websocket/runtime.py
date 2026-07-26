"""Campaign-level composition of public depth and private order streams."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from weex_cli.core.config import Credentials

from .contracts import OrderBookGateway
from .private_stream import WeexPrivateOrderStream
from .public_stream import WeexPublicOrderBookStream


class WeexCampaignWebSocketRuntime:
    """Campaign-scoped public and private WebSocket connections."""

    def __init__(
        self,
        snapshot_gateway: OrderBookGateway,
        credentials: Credentials,
        *,
        proxy_url: str | None = None,
        public_connect_factory: Callable[..., Any] | None = None,
        private_connect_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.public = WeexPublicOrderBookStream(
            snapshot_gateway, proxy_url=proxy_url, connect_factory=public_connect_factory
        )
        self.private = WeexPrivateOrderStream(credentials, proxy_url=proxy_url, connect_factory=private_connect_factory)

    def start(self) -> None:
        self.public.start()
        self.private.start()

    def close(self) -> None:
        self.private.close()
        self.public.close()

    def order_book(self, symbol: str, limit: int = 5) -> dict[str, Any]:
        return self.public.order_book(symbol, limit)

    def order_update(self, order_id: str, client_order_id: str) -> Mapping[str, Any] | None:
        return self.private.order_update(order_id, client_order_id)

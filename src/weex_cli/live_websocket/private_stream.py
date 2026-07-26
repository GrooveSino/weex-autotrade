"""WEEX private order update stream with conservative REST fallback behavior."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from weex_cli.core.config import Credentials
from weex_cli.presentation.i18n import text

from .contracts import WEEX_PRIVATE_WS_PATH, WEEX_PRIVATE_WS_URL, MarketStreamUnavailable
from .helpers import (
    json_payload,
    retry_delay,
    should_log_reconnect_error,
    socks_proxy_needs_unavailable_dependency,
    websocket_connect,
)

LOGGER = logging.getLogger(__name__)


class WeexPrivateOrderStream:
    """Cache private order lifecycle updates while REST remains authoritative."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        url: str = WEEX_PRIVATE_WS_URL,
        proxy_url: str | None = None,
        connect_factory: Callable[..., Any] | None = None,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.credentials = credentials
        self.url = url
        self.proxy_url = proxy_url
        self.connect_factory = connect_factory or websocket_connect
        self.clock_ms = clock_ms
        self.monotonic = monotonic
        self._orders_by_id: dict[str, dict[str, Any]] = {}
        self._orders_by_client_id: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._consecutive_errors = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if socks_proxy_needs_unavailable_dependency(self.proxy_url):
            LOGGER.info(
                text(
                    "WEEX 私有 WebSocket 使用 SOCKS 代理但同步适配器不可用；将安全回退到 REST",
                    "WEEX private WebSocket SOCKS adapter unavailable; safely falling back to REST",
                )
            )
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="weex-private-orders", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._mark_disconnected()

    def order_update(self, order_id: str, client_order_id: str) -> Mapping[str, Any] | None:
        with self._lock:
            if not self._connected:
                return None
            row = self._orders_by_id.get(order_id) or self._orders_by_client_id.get(client_order_id)
            return dict(row) if row is not None else None

    @staticmethod
    def subscription_message() -> str:
        return json.dumps(
            {"method": "SUBSCRIBE", "params": ["orders", "fill", "positions"], "id": 1}, separators=(",", ":")
        )

    def headers(self) -> dict[str, str]:
        timestamp = str(self.clock_ms())
        digest = hmac.new(
            self.credentials.api_secret.encode(), f"{timestamp}{WEEX_PRIVATE_WS_PATH}".encode(), hashlib.sha256
        ).digest()
        return {
            "User-Agent": "weex-autotrade/0.1",
            "ACCESS-KEY": self.credentials.api_key,
            "ACCESS-PASSPHRASE": self.credentials.passphrase,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-SIGN": base64.b64encode(digest).decode(),
        }

    def handle_message(self, websocket: Any, raw_message: str | bytes) -> None:
        payload = json_payload(raw_message)
        if payload is None:
            return
        if payload.get("type") == "ping":
            websocket.send('{"method":"PONG","id":1}')
            return
        if "result" in payload:
            if payload.get("result") is not True:
                raise MarketStreamUnavailable(
                    f"WEEX private subscription failed: {payload.get('msg') or 'unknown error'}"
                )
            self._consecutive_errors = 0
            return
        if payload.get("e") != "orders" or not isinstance(payload.get("d"), list):
            return
        with self._lock:
            for raw_row in payload["d"]:
                if not isinstance(raw_row, Mapping):
                    continue
                row = normalized_order_update(raw_row)
                if order_id := str(row.get("id") or ""):
                    self._orders_by_id[order_id] = row
                if client_id := str(row.get("clientOrderId") or ""):
                    self._orders_by_client_id[client_id] = row
            self._consecutive_errors = 0

    def _mark_connected(self) -> None:
        with self._lock:
            self._connected = True

    def _mark_disconnected(self) -> None:
        with self._lock:
            self._connected = False
            self._orders_by_id.clear()
            self._orders_by_client_id.clear()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self.connect_factory(
                    self.url,
                    open_timeout=15,
                    close_timeout=5,
                    ping_interval=None,
                    proxy=self.proxy_url,
                    additional_headers=self.headers(),
                ) as websocket:
                    self._mark_connected()
                    websocket.send(self.subscription_message())
                    while not self._stop.is_set():
                        try:
                            self.handle_message(websocket, websocket.recv(timeout=1))
                        except TimeoutError:
                            continue
            except Exception as exc:  # noqa: BLE001 - REST stays authoritative while reconnecting
                self._consecutive_errors += 1
                if should_log_reconnect_error(self._consecutive_errors):
                    LOGGER.warning(
                        text("WEEX 私有 WebSocket 暂不可用：%s", "WEEX private WebSocket unavailable: %s"),
                        type(exc).__name__,
                    )
            finally:
                self._mark_disconnected()
            self._stop.wait(retry_delay(self._consecutive_errors))


def normalized_order_update(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id") or ""),
        "clientOrderId": str(row.get("clientOrderId") or ""),
        "side": str(row.get("orderSide") or "").lower(),
        "status": str(row.get("status") or "").lower(),
        "amount": row.get("size") or 0,
        "filled": row.get("cumFillSize") or 0,
        "cost": row.get("cumFillValue") or 0,
        "price": row.get("price") or 0,
        "timeInForce": row.get("timeInForce") or "",
        "postOnly": str(row.get("timeInForce") or "").upper() == "POST_ONLY",
        "info": dict(row),
    }

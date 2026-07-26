"""Shared logging, stop and reconnection helpers for tick collectors."""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType
from typing import Any

from weex_cli.presentation.i18n import text

LOGGER = logging.getLogger(__name__)


def install_stop_handlers(stop_event: threading.Event) -> None:
    def request_stop(signum: int, _frame: FrameType | None) -> None:
        LOGGER.info(text("收到停止请求 信号=%d", "stop_requested signal=%d"), signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def retry_delay(consecutive_errors: int) -> float:
    if consecutive_errors <= 0:
        return 0.0
    return min(60.0, float(2 ** min(consecutive_errors - 1, 6)))


def websocket_connect(url: str, **kwargs: Any) -> Any:
    from websockets.sync.client import connect

    return connect(url, **kwargs)

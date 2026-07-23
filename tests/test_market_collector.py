from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from weex_cli.errors import ValidationError
from weex_cli.market_collector import (
    MarketCollector,
    Tick,
    TickStore,
    WebSocketMarketCollector,
    run_market_collector,
    run_websocket_market_collector,
)


class FakeGateway:
    def __init__(self, prices: dict[str, object]) -> None:
        self.prices = prices
        self.calls: list[str] = []

    def ticker(self, symbol: str) -> dict[str, object]:
        self.calls.append(symbol)
        return {"last": self.prices[symbol]}


class FakeWebSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = list(messages)
        self.sent: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def send(self, message: str) -> None:
        self.sent.append(message)

    def recv(self, timeout: float) -> str:
        if not self.messages:
            raise TimeoutError
        return self.messages.pop(0)


class AdvancingMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 1.0
        return self.value


def read_rows(db_path: Path) -> list[tuple[str, float, float, str]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute("SELECT symbol, price, timestamp, created_at FROM ticks ORDER BY symbol").fetchall()


def test_collect_once_writes_atomic_aligned_ticks(tmp_path: Path) -> None:
    db_path = tmp_path / "weex.db"
    gateway = FakeGateway({"BTC": "64123.4", "ETH": 1840.5})
    with TickStore(db_path) as store:
        collector = MarketCollector(gateway, store, ("BTC", "ETH"), clock=lambda: 1_784_314_083.25)
        result = collector.collect_once()

    assert gateway.calls == ["BTC", "ETH"]
    assert result.rows_written == 2
    assert read_rows(db_path) == [
        ("BTCUSDT", 64123.4, 1_784_314_083.25, "2026-07-18T02:48:03.250+08:00"),
        ("ETHUSDT", 1840.5, 1_784_314_083.25, "2026-07-18T02:48:03.250+08:00"),
    ]


def test_invalid_second_price_does_not_write_partial_cycle(tmp_path: Path) -> None:
    db_path = tmp_path / "weex.db"
    gateway = FakeGateway({"BTC": 64000, "ETH": None})
    with TickStore(db_path) as store:
        collector = MarketCollector(gateway, store, ("BTC", "ETH"))
        with pytest.raises(ValidationError, match="ETH"):
            collector.collect_once()

    assert read_rows(db_path) == []


def test_cleanup_preserves_only_retention_window(tmp_path: Path) -> None:
    db_path = tmp_path / "weex.db"
    now = 100_000.0
    with TickStore(db_path, retention_hours=12) as store:
        store.write((Tick("BTCUSDT", 1.0),), captured_at=now - 12 * 3600 - 0.1)
        store.write((Tick("BTCUSDT", 2.0),), captured_at=now - 12 * 3600)
        assert store.cleanup(now=now) == 1

    rows = read_rows(db_path)
    assert [(row[1], row[2]) for row in rows] == [(2.0, now - 12 * 3600)]


def test_once_mode_reports_one_cycle_and_runs_cleanup(tmp_path: Path) -> None:
    db_path = tmp_path / "weex.db"
    with TickStore(db_path) as store:
        collector = MarketCollector(
            FakeGateway({"BTC": 64000, "ETH": 1800}),
            store,
            ("BTC", "ETH"),
        )
        stats = run_market_collector(
            collector,
            once=True,
            stop_event=threading.Event(),
        )

    assert stats.cycles == 1
    assert stats.rows_written == 2
    assert stats.errors == 0


def test_websocket_collector_subscribes_pongs_and_writes_aligned_snapshot(
    tmp_path: Path,
) -> None:
    socket = FakeWebSocket(
        [
            '{"result":true,"id":1}',
            '{"event":"ping","time":"1784314083000"}',
            '{"e":"ticker","s":"BTCUSDT","d":[{"c":"64271.1"}]}',
            '{"e":"ticker","s":"ETHUSDT","d":[{"c":"1846.79"}]}',
        ]
    )
    db_path = tmp_path / "stream.db"
    with TickStore(db_path) as store:
        collector = WebSocketMarketCollector(
            store,
            ("BTC", "ETH"),
            connect_factory=lambda *_args, **_kwargs: socket,
            clock=lambda: 1_784_314_083.25,
        )
        stats = run_websocket_market_collector(collector, once=True)

    assert stats.cycles == 1
    assert stats.rows_written == 2
    assert socket.sent[0] == ('{"method":"SUBSCRIBE","params":["BTCUSDT@ticker","ETHUSDT@ticker"],"id":1}')
    assert socket.sent[1] == '{"method":"PONG","id":1}'
    assert read_rows(db_path) == [
        ("BTCUSDT", 64271.1, 1_784_314_083.25, "2026-07-18T02:48:03.250+08:00"),
        ("ETHUSDT", 1846.79, 1_784_314_083.25, "2026-07-18T02:48:03.250+08:00"),
    ]


def test_websocket_subscription_failure_is_reported_without_rows(
    tmp_path: Path,
) -> None:
    socket = FakeWebSocket(['{"result":false,"id":1,"msg":"denied"}'])
    db_path = tmp_path / "stream.db"
    with TickStore(db_path) as store:
        collector = WebSocketMarketCollector(
            store,
            ("BTC", "ETH"),
            connect_factory=lambda *_args, **_kwargs: socket,
        )
        stats = run_websocket_market_collector(collector, once=True)

    assert stats.cycles == 0
    assert stats.errors == 1
    assert read_rows(db_path) == []


def test_websocket_zero_price_frame_keeps_last_valid_price(tmp_path: Path) -> None:
    socket = FakeWebSocket([])
    with TickStore(tmp_path / "stream.db") as store:
        collector = WebSocketMarketCollector(store, ("BTC", "ETH"))
        collector.handle_message(
            socket,
            '{"e":"ticker","s":"BTCUSDT","d":[{"c":"64026.4"}]}',
        )
        collector.handle_message(
            socket,
            '{"e":"ticker","s":"BTCUSDT","d":[{"c":"0","m":"64059.2"}]}',
        )

    assert collector.latest_prices["BTCUSDT"] == 64026.4
    assert collector.ignored_ticks == 1


def test_websocket_silence_is_reported_instead_of_reusing_old_prices(
    tmp_path: Path,
) -> None:
    socket = FakeWebSocket([])
    monotonic = AdvancingMonotonic()
    db_path = tmp_path / "stream.db"
    with TickStore(db_path) as store:
        collector = WebSocketMarketCollector(
            store,
            ("BTC", "ETH"),
            connect_factory=lambda *_args, **_kwargs: socket,
            monotonic=monotonic,
        )
        stats = run_websocket_market_collector(
            collector,
            stale_after_seconds=1.0,
            once=True,
        )

    assert stats.cycles == 0
    assert stats.rows_written == 0
    assert stats.errors == 1
    assert read_rows(db_path) == []

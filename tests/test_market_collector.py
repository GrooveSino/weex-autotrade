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
    run_market_collector,
)


class FakeGateway:
    def __init__(self, prices: dict[str, object]) -> None:
        self.prices = prices
        self.calls: list[str] = []

    def ticker(self, symbol: str) -> dict[str, object]:
        self.calls.append(symbol)
        return {"last": self.prices[symbol]}


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

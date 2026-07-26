"""SQLite persistence for public market snapshots."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from weex_cli.core.errors import ValidationError

from .contracts import Tick

CHINA_STANDARD_TIME = timezone(timedelta(hours=8))


class TickStore:
    """SQLite writer compatible with the weex-calc ticks table."""

    def __init__(self, db_path: Path, *, retention_hours: float = 12.0) -> None:
        if retention_hours <= 0:
            raise ValidationError("retention_hours must be greater than zero")
        self.db_path = db_path.expanduser().resolve()
        self.retention_seconds = retention_hours * 3600
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path, timeout=5.0)
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    price REAL NOT NULL,
                    timestamp REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(symbol, timestamp)
                )
                """
            )
            self.connection.execute("CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts ON ticks(symbol, timestamp)")
            self.connection.execute("CREATE INDEX IF NOT EXISTS idx_ticks_timestamp ON ticks(timestamp)")

    def write(self, ticks: tuple[Tick, ...], *, captured_at: float) -> int:
        if not ticks:
            return 0
        created_at = datetime.fromtimestamp(captured_at, tz=CHINA_STANDARD_TIME).isoformat(timespec="milliseconds")
        before = self.connection.total_changes
        with self.connection:
            self.connection.executemany(
                "INSERT OR IGNORE INTO ticks (symbol, price, timestamp, created_at) VALUES (?, ?, ?, ?)",
                ((tick.symbol, tick.price, captured_at, created_at) for tick in ticks),
            )
        return self.connection.total_changes - before

    def cleanup(self, *, now: float | None = None) -> int:
        cutoff = (time.time() if now is None else now) - self.retention_seconds
        with self.connection:
            cursor = self.connection.execute("DELETE FROM ticks WHERE timestamp < ?", (cutoff,))
        return max(0, cursor.rowcount)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> TickStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

"""SQLite storage for normalized local trade-volume history."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from weex_cli.core.models import decimal_text

from .contracts import DEMO_WINDOW_MS, CachedTrade, SyncState
from .support import COUNT_TOTAL_KEYS, DECIMAL_TOTAL_KEYS, TOTAL_KEYS, empty_totals


class SQLiteTradeVolumeLedger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def __enter__(self) -> SQLiteTradeVolumeLedger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;
            CREATE TABLE IF NOT EXISTS volume_trades (
                account_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                trade_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                quote_volume TEXT NOT NULL,
                action TEXT NOT NULL,
                liquidity TEXT NOT NULL,
                PRIMARY KEY (account_id, mode, trade_id)
            );
            CREATE INDEX IF NOT EXISTS volume_trades_time
                ON volume_trades(account_id, mode, timestamp_ms);
            CREATE INDEX IF NOT EXISTS volume_trades_symbol_time
                ON volume_trades(account_id, mode, symbol, timestamp_ms);
            CREATE TABLE IF NOT EXISTS volume_totals (
                account_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                symbol TEXT NOT NULL,
                total_quote TEXT NOT NULL,
                opening_quote TEXT NOT NULL,
                closing_quote TEXT NOT NULL,
                unknown_action_quote TEXT NOT NULL,
                maker_quote TEXT NOT NULL,
                taker_quote TEXT NOT NULL,
                unknown_liquidity_quote TEXT NOT NULL,
                trade_count INTEGER NOT NULL,
                maker_count INTEGER NOT NULL,
                taker_count INTEGER NOT NULL,
                unknown_liquidity_count INTEGER NOT NULL,
                PRIMARY KEY (account_id, mode, symbol)
            );
            CREATE TABLE IF NOT EXISTS volume_sync_state (
                account_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                symbol TEXT NOT NULL,
                history_start_ms INTEGER NOT NULL,
                backfill_end_ms INTEGER NOT NULL,
                cursor_ms INTEGER NOT NULL,
                next_page INTEGER NOT NULL,
                backfill_complete INTEGER NOT NULL,
                last_poll_ms INTEGER NOT NULL,
                PRIMARY KEY (account_id, mode, symbol)
            );
            CREATE TABLE IF NOT EXISTS volume_sync_windows (
                account_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                symbol TEXT NOT NULL,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL,
                PRIMARY KEY (account_id, mode, symbol, start_ms, end_ms)
            );
            CREATE TABLE IF NOT EXISTS volume_sync_gaps (
                account_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                symbol TEXT NOT NULL,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL,
                reason TEXT NOT NULL,
                PRIMARY KEY (account_id, mode, symbol, start_ms, end_ms)
            );
            """
        )
        self.connection.commit()

    def state(self, account_id: str, mode: str, symbol: str) -> SyncState | None:
        row = self.connection.execute(
            """SELECT history_start_ms, backfill_end_ms, cursor_ms, next_page,
                      backfill_complete, last_poll_ms
               FROM volume_sync_state
               WHERE account_id = ? AND mode = ? AND symbol = ?""",
            (account_id, mode, symbol),
        ).fetchone()
        if row is None:
            return None
        return SyncState(
            history_start_ms=int(row["history_start_ms"]),
            backfill_end_ms=int(row["backfill_end_ms"]),
            cursor_ms=int(row["cursor_ms"]),
            next_page=int(row["next_page"]),
            backfill_complete=bool(row["backfill_complete"]),
            last_poll_ms=int(row["last_poll_ms"]),
        )

    def ensure_state(self, account_id: str, mode: str, symbol: str, start_ms: int, end_ms: int) -> SyncState:
        current = self.state(account_id, mode, symbol)
        if current is None:
            current = SyncState(start_ms, end_ms, start_ms, 0, False, 0)
            self.save_state(account_id, mode, symbol, current)
            self._seed_windows(account_id, mode, symbol, start_ms, end_ms)
        elif start_ms < current.history_start_ms:
            previous_start = current.history_start_ms
            current = SyncState(start_ms, max(end_ms, current.backfill_end_ms), start_ms, 0, False, 0)
            self.save_state(account_id, mode, symbol, current)
            self._seed_windows(account_id, mode, symbol, start_ms, previous_start - 1)
        return current

    def _seed_windows(self, account_id: str, mode: str, symbol: str, start_ms: int, end_ms: int) -> None:
        cursor = start_ms
        with self.connection:
            while cursor <= end_ms:
                window_end = min(cursor + DEMO_WINDOW_MS - 1, end_ms)
                self.connection.execute(
                    """INSERT OR IGNORE INTO volume_sync_windows
                       (account_id, mode, symbol, start_ms, end_ms) VALUES (?, ?, ?, ?, ?)""",
                    (account_id, mode, symbol, cursor, window_end),
                )
                cursor = window_end + 1

    def next_window(self, account_id: str, mode: str, symbol: str) -> tuple[int, int] | None:
        row = self.connection.execute(
            """SELECT start_ms, end_ms FROM volume_sync_windows
               WHERE account_id = ? AND mode = ? AND symbol = ?
               ORDER BY start_ms, end_ms LIMIT 1""",
            (account_id, mode, symbol),
        ).fetchone()
        return None if row is None else (int(row["start_ms"]), int(row["end_ms"]))

    def split_window(self, account_id: str, mode: str, symbol: str, start_ms: int, end_ms: int) -> None:
        midpoint = (start_ms + end_ms) // 2
        with self.connection:
            self._delete_window(account_id, mode, symbol, start_ms, end_ms)
            self.connection.executemany(
                """INSERT OR IGNORE INTO volume_sync_windows
                   (account_id, mode, symbol, start_ms, end_ms) VALUES (?, ?, ?, ?, ?)""",
                (
                    (account_id, mode, symbol, start_ms, midpoint),
                    (account_id, mode, symbol, midpoint + 1, end_ms),
                ),
            )

    def finish_window(self, account_id: str, mode: str, symbol: str, start_ms: int, end_ms: int) -> None:
        with self.connection:
            self._delete_window(account_id, mode, symbol, start_ms, end_ms)

    def mark_gap(self, account_id: str, mode: str, symbol: str, start_ms: int, end_ms: int) -> None:
        with self.connection:
            self._delete_window(account_id, mode, symbol, start_ms, end_ms)
            self.connection.execute(
                """INSERT OR IGNORE INTO volume_sync_gaps
                   (account_id, mode, symbol, start_ms, end_ms, reason)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (account_id, mode, symbol, start_ms, end_ms, "1000_or_more_orders_in_one_millisecond"),
            )

    def gap_count(self, account_id: str, mode: str, symbol: str) -> int:
        row = self.connection.execute(
            """SELECT COUNT(*) AS count FROM volume_sync_gaps
               WHERE account_id = ? AND mode = ? AND symbol = ?""",
            (account_id, mode, symbol),
        ).fetchone()
        return int(row["count"])

    def _delete_window(self, account_id: str, mode: str, symbol: str, start_ms: int, end_ms: int) -> None:
        self.connection.execute(
            """DELETE FROM volume_sync_windows
               WHERE account_id = ? AND mode = ? AND symbol = ? AND start_ms = ? AND end_ms = ?""",
            (account_id, mode, symbol, start_ms, end_ms),
        )

    def save_state(self, account_id: str, mode: str, symbol: str, state: SyncState) -> None:
        self.connection.execute(
            """INSERT INTO volume_sync_state(
                   account_id, mode, symbol, history_start_ms, backfill_end_ms,
                   cursor_ms, next_page, backfill_complete, last_poll_ms
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_id, mode, symbol) DO UPDATE SET
                   history_start_ms = excluded.history_start_ms,
                   backfill_end_ms = excluded.backfill_end_ms,
                   cursor_ms = excluded.cursor_ms,
                   next_page = excluded.next_page,
                   backfill_complete = excluded.backfill_complete,
                   last_poll_ms = excluded.last_poll_ms""",
            (
                account_id,
                mode,
                symbol,
                state.history_start_ms,
                state.backfill_end_ms,
                state.cursor_ms,
                state.next_page,
                int(state.backfill_complete),
                state.last_poll_ms,
            ),
        )
        self.connection.commit()

    def record(self, account_id: str, mode: str, trades: list[CachedTrade]) -> tuple[int, int, int]:
        inserted = updated = unchanged = 0
        with self.connection:
            for trade in trades:
                old = self.connection.execute(
                    """SELECT order_id, symbol, timestamp_ms, quote_volume, action, liquidity
                       FROM volume_trades WHERE account_id = ? AND mode = ? AND trade_id = ?""",
                    (account_id, mode, trade.trade_id),
                ).fetchone()
                values = (
                    trade.order_id,
                    trade.symbol,
                    trade.timestamp,
                    decimal_text(trade.quote_volume),
                    trade.action,
                    trade.liquidity,
                )
                if old is not None and tuple(old) == values:
                    unchanged += 1
                    continue
                if old is not None:
                    previous = CachedTrade(
                        trade_id=trade.trade_id,
                        order_id=str(old["order_id"]),
                        symbol=str(old["symbol"]),
                        timestamp=int(old["timestamp_ms"]),
                        quote_volume=Decimal(str(old["quote_volume"])),
                        action=str(old["action"]),
                        liquidity=str(old["liquidity"]),
                    )
                    self._adjust(account_id, mode, previous, -1)
                    updated += 1
                else:
                    inserted += 1
                self.connection.execute(
                    """INSERT INTO volume_trades(
                           account_id, mode, trade_id, order_id, symbol, timestamp_ms,
                           quote_volume, action, liquidity
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(account_id, mode, trade_id) DO UPDATE SET
                           order_id = excluded.order_id,
                           symbol = excluded.symbol,
                           timestamp_ms = excluded.timestamp_ms,
                           quote_volume = excluded.quote_volume,
                           action = excluded.action,
                           liquidity = excluded.liquidity""",
                    (account_id, mode, trade.trade_id, *values),
                )
                self._adjust(account_id, mode, trade, 1)
        return inserted, updated, unchanged

    def _adjust(self, account_id: str, mode: str, trade: CachedTrade, direction: int) -> None:
        for symbol in ("*", trade.symbol):
            row = self.connection.execute(
                "SELECT * FROM volume_totals WHERE account_id = ? AND mode = ? AND symbol = ?",
                (account_id, mode, symbol),
            ).fetchone()
            totals = empty_totals() if row is None else {key: row[key] for key in TOTAL_KEYS}
            quote_delta = trade.quote_volume * direction
            totals["total_quote"] = Decimal(str(totals["total_quote"])) + quote_delta
            action_key = f"{trade.action}_quote" if trade.action in {"opening", "closing"} else "unknown_action_quote"
            totals[action_key] = Decimal(str(totals[action_key])) + quote_delta
            totals[f"{trade.liquidity}_quote"] = Decimal(str(totals[f"{trade.liquidity}_quote"])) + quote_delta
            totals["trade_count"] = int(totals["trade_count"]) + direction
            totals[f"{trade.liquidity}_count"] = int(totals[f"{trade.liquidity}_count"]) + direction
            self.connection.execute(
                """INSERT INTO volume_totals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(account_id, mode, symbol) DO UPDATE SET
                       total_quote = excluded.total_quote,
                       opening_quote = excluded.opening_quote,
                       closing_quote = excluded.closing_quote,
                       unknown_action_quote = excluded.unknown_action_quote,
                       maker_quote = excluded.maker_quote,
                       taker_quote = excluded.taker_quote,
                       unknown_liquidity_quote = excluded.unknown_liquidity_quote,
                       trade_count = excluded.trade_count,
                       maker_count = excluded.maker_count,
                       taker_count = excluded.taker_count,
                       unknown_liquidity_count = excluded.unknown_liquidity_count""",
                (
                    account_id,
                    mode,
                    symbol,
                    *[decimal_text(Decimal(str(totals[key]))) for key in DECIMAL_TOTAL_KEYS],
                    *[int(totals[key]) for key in COUNT_TOTAL_KEYS],
                ),
            )

    def summary(self, account_id: str, mode: str, symbol: str | None = None) -> dict[str, Any]:
        key = symbol.upper() if symbol else "*"
        row = self.connection.execute(
            "SELECT * FROM volume_totals WHERE account_id = ? AND mode = ? AND symbol = ?",
            (account_id, mode, key),
        ).fetchone()
        totals = empty_totals() if row is None else {name: row[name] for name in TOTAL_KEYS}
        clause = " AND symbol = ?" if symbol else ""
        params: tuple[Any, ...] = (account_id, mode, key) if symbol else (account_id, mode)
        bounds = self.connection.execute(
            f"SELECT MIN(timestamp_ms) AS first_ms, MAX(timestamp_ms) AS last_ms "
            f"FROM volume_trades WHERE account_id = ? AND mode = ?{clause}",
            params,
        ).fetchone()
        return {
            "quote_asset": "SUSDT" if mode == "demo" else "USDT",
            "total_quote_volume": decimal_text(Decimal(str(totals["total_quote"]))),
            "opening_quote_volume": decimal_text(Decimal(str(totals["opening_quote"]))),
            "closing_quote_volume": decimal_text(Decimal(str(totals["closing_quote"]))),
            "unknown_action_quote_volume": decimal_text(Decimal(str(totals["unknown_action_quote"]))),
            "maker_quote_volume": decimal_text(Decimal(str(totals["maker_quote"]))),
            "taker_quote_volume": decimal_text(Decimal(str(totals["taker_quote"]))),
            "unknown_liquidity_quote_volume": decimal_text(Decimal(str(totals["unknown_liquidity_quote"]))),
            "trade_count": int(totals["trade_count"]),
            "maker_count": int(totals["maker_count"]),
            "taker_count": int(totals["taker_count"]),
            "unknown_liquidity_count": int(totals["unknown_liquidity_count"]),
            "first_trade_time": bounds["first_ms"] if bounds else None,
            "last_trade_time": bounds["last_ms"] if bounds else None,
        }

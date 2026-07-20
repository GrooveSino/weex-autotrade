from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from weex_cli.errors import ValidationError
from weex_cli.models import decimal_text
from weex_cli.symbols import demo_symbol_id

DAY_MS = 24 * 60 * 60 * 1000
DEMO_WINDOW_MS = 90 * DAY_MS
DEMO_PAGE_LIMIT = 1000


class TradeHistoryGateway(Protocol):
    def trade_rows(
        self,
        mode: str,
        symbol: str | None,
        *,
        start_time: int,
        end_time: int,
        limit: int,
        page: int | None = None,
    ) -> list[dict[str, Any]]: ...


class TradeVolumeRateLimited(RuntimeError):
    pass


@dataclass(frozen=True)
class CachedTrade:
    trade_id: str
    order_id: str
    symbol: str
    timestamp: int
    quote_volume: Decimal
    action: str
    liquidity: str


@dataclass(frozen=True)
class SyncState:
    history_start_ms: int
    backfill_end_ms: int
    cursor_ms: int
    next_page: int
    backfill_complete: bool
    last_poll_ms: int


def account_fingerprint(api_key: str) -> str:
    if not api_key:
        raise ValidationError("An API key is required to isolate the local volume cache")
    return hashlib.sha256(api_key.encode()).hexdigest()[:24]


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
            totals = _empty_totals() if row is None else {key: row[key] for key in _TOTAL_KEYS}
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
                    *[decimal_text(Decimal(str(totals[key]))) for key in _DECIMAL_TOTAL_KEYS],
                    *[int(totals[key]) for key in _COUNT_TOTAL_KEYS],
                ),
            )

    def summary(self, account_id: str, mode: str, symbol: str | None = None) -> dict[str, Any]:
        key = symbol.upper() if symbol else "*"
        row = self.connection.execute(
            "SELECT * FROM volume_totals WHERE account_id = ? AND mode = ? AND symbol = ?",
            (account_id, mode, key),
        ).fetchone()
        totals = _empty_totals() if row is None else {name: row[name] for name in _TOTAL_KEYS}
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


class DemoTradeVolumeSyncService:
    def __init__(self, gateway: TradeHistoryGateway, ledger: SQLiteTradeVolumeLedger, account_id: str) -> None:
        self.gateway = gateway
        self.ledger = ledger
        self.account_id = account_id

    def sync(
        self,
        *,
        start_time: int,
        end_time: int,
        symbol: str | None = None,
        max_requests: int = 50,
        overlap_ms: int = 60_000,
    ) -> dict[str, Any]:
        if start_time < 0 or end_time < start_time:
            raise ValidationError("Invalid volume sync time range")
        if max_requests < 1:
            raise ValidationError("max_requests must be positive")
        symbol_key = demo_symbol_id(symbol) if symbol else "*"
        state = self.ledger.ensure_state(self.account_id, "demo", symbol_key, start_time, end_time)
        requests = inserted = updated = unchanged = 0
        rate_limited = False

        if not state.backfill_complete:
            while requests < max_requests:
                window = self.ledger.next_window(self.account_id, "demo", symbol_key)
                if window is None:
                    state = SyncState(
                        state.history_start_ms,
                        state.backfill_end_ms,
                        state.backfill_end_ms + 1,
                        0,
                        True,
                        state.backfill_end_ms,
                    )
                    self.ledger.save_state(self.account_id, "demo", symbol_key, state)
                    break
                window_start, window_end = window
                try:
                    batch = self._fetch(symbol, window_start, window_end, 0)
                except TradeVolumeRateLimited:
                    requests += 1
                    rate_limited = True
                    break
                else:
                    requests += 1
                if len(batch) >= DEMO_PAGE_LIMIT:
                    if window_start < window_end:
                        self.ledger.split_window(self.account_id, "demo", symbol_key, window_start, window_end)
                    else:
                        counts = self.ledger.record(self.account_id, "demo", _normalize_demo_rows(batch))
                        inserted += counts[0]
                        updated += counts[1]
                        unchanged += counts[2]
                        self.ledger.mark_gap(self.account_id, "demo", symbol_key, window_start, window_end)
                else:
                    counts = self.ledger.record(self.account_id, "demo", _normalize_demo_rows(batch))
                    inserted += counts[0]
                    updated += counts[1]
                    unchanged += counts[2]
                    self.ledger.finish_window(self.account_id, "demo", symbol_key, window_start, window_end)

        poll_complete = True
        if not rate_limited and state.backfill_complete and end_time > state.last_poll_ms and requests < max_requests:
            poll_start = max(state.history_start_ms, state.last_poll_ms - overlap_ms)
            pending = [(poll_start, end_time)]
            while pending and requests < max_requests:
                window_start, window_end = pending.pop()
                try:
                    batch = self._fetch(symbol, window_start, window_end, 0)
                except TradeVolumeRateLimited:
                    requests += 1
                    rate_limited = True
                    poll_complete = False
                    break
                else:
                    requests += 1
                if len(batch) >= DEMO_PAGE_LIMIT and window_start < window_end:
                    midpoint = (window_start + window_end) // 2
                    pending.extend(((window_start, midpoint), (midpoint + 1, window_end)))
                    continue
                counts = self.ledger.record(self.account_id, "demo", _normalize_demo_rows(batch))
                inserted += counts[0]
                updated += counts[1]
                unchanged += counts[2]
                if len(batch) >= DEMO_PAGE_LIMIT:
                    self.ledger.mark_gap(self.account_id, "demo", symbol_key, window_start, window_end)
            if pending:
                poll_complete = False
            elif not rate_limited:
                state = SyncState(
                    state.history_start_ms,
                    state.backfill_end_ms,
                    state.cursor_ms,
                    0,
                    True,
                    end_time,
                )
                self.ledger.save_state(self.account_id, "demo", symbol_key, state)

        gaps = self.ledger.gap_count(self.account_id, "demo", symbol_key)
        complete = state.backfill_complete and poll_complete and not rate_limited and gaps == 0
        return {
            "status": "rate_limited" if rate_limited else "completed" if complete else "partial",
            "mode": "demo",
            "source": "demo_order_history_incremental_cache",
            "network_requests": requests,
            "inserted_trades": inserted,
            "updated_trades": updated,
            "unchanged_trades": unchanged,
            "history_complete": state.backfill_complete and gaps == 0,
            "ambiguous_windows": gaps,
            "coverage_start_time": state.history_start_ms,
            "last_sync_time": state.last_poll_ms,
            "retry_after_seconds": 10 if rate_limited else None,
            "summary": self.ledger.summary(self.account_id, "demo", None if symbol is None else symbol_key),
        }

    def _fetch(self, symbol: str | None, start_time: int, end_time: int, page: int) -> list[dict[str, Any]]:
        try:
            rows = self.gateway.trade_rows(
                "demo",
                symbol,
                start_time=start_time,
                end_time=end_time,
                limit=DEMO_PAGE_LIMIT,
                page=page,
            )
        except Exception as exc:
            text = str(exc).lower()
            if "-1003" in text or "too much request weight" in text:
                raise TradeVolumeRateLimited from exc
            raise
        if not isinstance(rows, list):
            raise ValidationError("Demo order history returned a non-list response")
        return rows


_DECIMAL_TOTAL_KEYS = (
    "total_quote",
    "opening_quote",
    "closing_quote",
    "unknown_action_quote",
    "maker_quote",
    "taker_quote",
    "unknown_liquidity_quote",
)
_COUNT_TOTAL_KEYS = ("trade_count", "maker_count", "taker_count", "unknown_liquidity_count")
_TOTAL_KEYS = (*_DECIMAL_TOTAL_KEYS, *_COUNT_TOTAL_KEYS)


def _empty_totals() -> dict[str, Decimal | int]:
    return {**{key: Decimal(0) for key in _DECIMAL_TOTAL_KEYS}, **{key: 0 for key in _COUNT_TOTAL_KEYS}}


def _normalize_demo_rows(rows: list[dict[str, Any]]) -> list[CachedTrade]:
    normalized: list[CachedTrade] = []
    for row in rows:
        quantity = _decimal(row.get("executedQty"))
        if quantity <= 0:
            continue
        quote = _decimal(row.get("cumQuote"))
        if quote <= 0:
            quote = quantity * _decimal(row.get("avgPrice") or row.get("price"))
        timestamp = _integer(row.get("updateTime") or row.get("time"))
        order_id = str(row.get("orderId") or row.get("id") or "")
        if quote <= 0 or timestamp is None or not order_id:
            continue
        normalized.append(
            CachedTrade(
                trade_id=order_id,
                order_id=order_id,
                symbol=str(row.get("symbol") or "UNKNOWN").upper(),
                timestamp=timestamp,
                quote_volume=quote,
                action=_position_action(row),
                liquidity="maker" if str(row.get("timeInForce") or "").upper() == "POST_ONLY" else "unknown_liquidity",
            )
        )
    return normalized


def _position_action(row: dict[str, Any]) -> str:
    side = str(row.get("side") or "").upper()
    position_side = str(row.get("positionSide") or "").upper()
    if (side, position_side) in {("BUY", "LONG"), ("SELL", "SHORT")}:
        return "opening"
    if (side, position_side) in {("SELL", "LONG"), ("BUY", "SHORT")}:
        return "closing"
    return "unknown_action"


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _integer(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Protocol
from zoneinfo import ZoneInfo

from .models import AccountInstance
from .vault import CredentialMaterial

SHANGHAI = ZoneInfo("Asia/Shanghai")


class FillConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NormalizedTradeFill:
    identity: str
    executed_at_ms: int
    quote_volume: Decimal
    symbol: str
    order_id: str = ""
    base_quantity: Decimal = Decimal(0)
    side: str = ""
    position_side: str = ""
    position_action: str = "unknown"
    maker: bool | None = None
    commission: Decimal = Decimal(0)
    commission_asset: str | None = None
    realized_pnl: Decimal = Decimal(0)
    source: str = "user_trades"
    authoritative: bool = True
    created_at_ms: int | None = None

    def __post_init__(self) -> None:
        if not self.identity.strip():
            raise ValueError("fill identity cannot be empty")
        if self.executed_at_ms < 0:
            raise ValueError("fill timestamp cannot be negative")
        if not self.quote_volume.is_finite() or self.quote_volume < 0:
            raise ValueError("fill quote volume must be finite and non-negative")
        if not self.symbol.strip():
            raise ValueError("fill symbol cannot be empty")
        if self.created_at_ms is not None and self.created_at_ms < 0:
            raise ValueError("fill creation timestamp cannot be negative")
        for value, name in (
            (self.base_quantity, "base quantity"),
            (self.commission, "commission"),
            (self.realized_pnl, "realized pnl"),
        ):
            if not value.is_finite():
                raise ValueError(f"fill {name} must be finite")


@dataclass(frozen=True, slots=True)
class TradeHistoryPage:
    fills: tuple[NormalizedTradeFill, ...]
    next_cursor: str | None
    complete: bool = True
    high_watermark_ms: int | None = None


@dataclass(frozen=True, slots=True)
class TradeHistoryContext:
    instance: AccountInstance
    credentials: CredentialMaterial | None


@dataclass(frozen=True, slots=True)
class TradeVolumeAggregate:
    lifetime: Decimal
    today: Decimal
    fill_count: int
    complete: bool


@dataclass(frozen=True, slots=True)
class TradeHistorySyncResult:
    aggregate: TradeVolumeAggregate
    pages_fetched: int
    fills_inserted: int
    stop_reason: str
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class VolumeSession:
    session_id: str
    account_id: str
    mode: str
    started_at_ms: int
    target_quote_volume: Decimal
    status: str
    verified_quote_volume: Decimal
    remaining_quote_volume: Decimal
    last_sync_at_ms: int | None
    last_reconciliation_at_ms: int | None
    source_complete: bool
    stale: bool
    reconciliation_required: bool
    discrepancy_quote_volume: Decimal
    cursor: str | None
    high_watermark_ms: int | None
    pending_sync: bool
    maker_only_required: bool
    uncertain_order_state: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "account_id": self.account_id,
            "mode": self.mode,
            "started_at_ms": self.started_at_ms,
            "target_quote_volume": str(self.target_quote_volume),
            "status": self.status,
            "verified_quote_volume": str(self.verified_quote_volume),
            "remaining_quote_volume": str(self.remaining_quote_volume),
            "last_sync_at_ms": self.last_sync_at_ms,
            "last_reconciliation_at_ms": self.last_reconciliation_at_ms,
            "source_complete": self.source_complete,
            "stale": self.stale,
            "reconciliation_required": self.reconciliation_required,
            "discrepancy_quote_volume": str(self.discrepancy_quote_volume),
            "cursor": self.cursor,
            "high_watermark_ms": self.high_watermark_ms,
            "pending_sync": self.pending_sync,
            "maker_only_required": self.maker_only_required,
            "uncertain_order_state": self.uncertain_order_state,
        }


class TradeHistorySource(Protocol):
    async def fetch_page(
        self,
        context: TradeHistoryContext,
        *,
        cursor: str | None,
        limit: int,
    ) -> TradeHistoryPage: ...


class TradeVolumeLedger(Protocol):
    def record(self, instance_id: str, fills: tuple[NormalizedTradeFill, ...]) -> int: ...

    def set_complete(self, instance_id: str, complete: bool) -> None: ...

    def aggregate(self, instance_id: str, today_start_ms: int) -> TradeVolumeAggregate: ...

    def remove(self, instance_id: str) -> None: ...

    def close(self) -> None: ...

    def create_session(
        self,
        session_id: str,
        account_id: str,
        mode: str,
        started_at_ms: int,
        target_quote_volume: Decimal,
        *,
        maker_only_required: bool = False,
    ) -> VolumeSession: ...

    def get_session(self, session_id: str) -> VolumeSession | None: ...

    def update_session(self, session_id: str, **changes: object) -> VolumeSession: ...

    def session_projection(self, session_id: str) -> dict[str, object]: ...

    def record_account_fills(
        self,
        account_id: str,
        mode: str,
        fills: tuple[NormalizedTradeFill, ...],
    ) -> int: ...

    def account_summary(self, account_id: str, mode: str) -> dict[str, object]: ...

    def save_sync_checkpoint(
        self,
        account_id: str,
        mode: str,
        *,
        cursor: str | None,
        high_watermark_ms: int | None,
        pending: bool,
        source_complete: bool,
        coverage_complete: bool,
        stale: bool,
    ) -> None: ...

    def sync_checkpoint(self, account_id: str, mode: str) -> dict[str, object] | None: ...

    def fills_for_account(
        self, account_id: str, mode: str, started_at_ms: int = 0
    ) -> tuple[NormalizedTradeFill, ...]: ...

    def refresh_sessions(
        self, account_id: str, mode: str, *, now_ms: int, source_complete: bool, stale: bool
    ) -> None: ...

    def latest_session(self, account_id: str, mode: str) -> dict[str, object] | None: ...

    def mark_sessions_reconciliation(
        self, account_id: str, mode: str, *, discrepancy: Decimal = Decimal(0)
    ) -> None: ...


class InMemoryTradeVolumeLedger:
    def __init__(self) -> None:
        self._fills: dict[str, dict[str, NormalizedTradeFill]] = {}
        self._complete: dict[str, bool] = {}
        self._account_fills: dict[tuple[str, str], dict[str, NormalizedTradeFill]] = {}
        self._sessions: dict[str, VolumeSession] = {}
        self._checkpoints: dict[tuple[str, str], dict[str, object]] = {}
        self._lock = RLock()

    def record(self, instance_id: str, fills: tuple[NormalizedTradeFill, ...]) -> int:
        with self._lock:
            current = self._fills.setdefault(instance_id, {})
            proposed = dict(current)
            for fill in fills:
                existing = proposed.get(fill.identity)
                if existing is not None and existing != fill:
                    raise FillConflictError(f"fill identity {fill.identity!r} changed across history pages")
                proposed[fill.identity] = fill
            inserted = len(proposed) - len(current)
            self._fills[instance_id] = proposed
            return inserted

    def set_complete(self, instance_id: str, complete: bool) -> None:
        with self._lock:
            self._complete[instance_id] = complete

    def aggregate(self, instance_id: str, today_start_ms: int) -> TradeVolumeAggregate:
        with self._lock:
            fills = tuple(self._fills.get(instance_id, {}).values())
            complete = self._complete.get(instance_id, False)
        return _aggregate(fills, today_start_ms, complete)

    def remove(self, instance_id: str) -> None:
        with self._lock:
            self._fills.pop(instance_id, None)
            self._complete.pop(instance_id, None)

    def close(self) -> None:
        return None

    def record_account_fills(self, account_id: str, mode: str, fills: tuple[NormalizedTradeFill, ...]) -> int:
        with self._lock:
            current = self._account_fills.setdefault((account_id, mode), {})
            proposed = dict(current)
            now_ms = int(datetime.now(UTC).timestamp() * 1000)
            normalized = tuple(
                fill
                if fill.created_at_ms is not None
                else replace(
                    fill,
                    created_at_ms=(current[fill.identity].created_at_ms if fill.identity in current else now_ms),
                )
                for fill in fills
            )
            for fill in normalized:
                existing = proposed.get(fill.identity)
                if existing is not None and existing != fill:
                    raise FillConflictError(f"fill identity {fill.identity!r} changed across history pages")
                proposed[fill.identity] = fill
            inserted = len(proposed) - len(current)
            self._account_fills[(account_id, mode)] = proposed
            self.record(
                account_id,
                tuple(replace(fill, identity=f"{mode}:{fill.identity}") for fill in normalized),
            )
            return inserted

    def create_session(
        self,
        session_id: str,
        account_id: str,
        mode: str,
        started_at_ms: int,
        target_quote_volume: Decimal,
        *,
        maker_only_required: bool = False,
    ) -> VolumeSession:
        if started_at_ms < 0 or target_quote_volume <= 0 or not target_quote_volume.is_finite():
            raise ValueError("invalid volume session parameters")
        with self._lock:
            if session_id in self._sessions:
                raise ValueError(f"session {session_id!r} already exists")
            if any(
                session.account_id == account_id and session.mode == mode and session.status == "running"
                for session in self._sessions.values()
            ):
                raise ValueError("an account can have only one running volume session")
            session = VolumeSession(
                session_id,
                account_id,
                mode,
                started_at_ms,
                target_quote_volume,
                "running",
                Decimal(0),
                target_quote_volume,
                None,
                None,
                False,
                True,
                False,
                Decimal(0),
                None,
                None,
                True,
                maker_only_required,
                False,
            )
            self._sessions[session_id] = session
            return session

    def get_session(self, session_id: str) -> VolumeSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def update_session(self, session_id: str, **changes: object) -> VolumeSession:
        with self._lock:
            current = self._sessions[session_id]
            updated = replace(current, **changes)
            if "verified_quote_volume" in changes and "remaining_quote_volume" not in changes:
                updated = replace(
                    updated,
                    remaining_quote_volume=max(updated.target_quote_volume - updated.verified_quote_volume, Decimal(0)),
                )
            self._sessions[session_id] = updated
            return updated

    def _session_fills(self, session: VolumeSession) -> list[NormalizedTradeFill]:
        return [
            fill
            for fill in self._account_fills.get((session.account_id, session.mode), {}).values()
            if fill.executed_at_ms >= session.started_at_ms
        ]

    def session_projection(self, session_id: str) -> dict[str, object]:
        with self._lock:
            session = self._sessions[session_id]
            fills = self._session_fills(session)
            eligible = [fill for fill in fills if fill.authoritative]
            verified = sum((fill.quote_volume for fill in eligible), Decimal(0))
            if session.stale or session.reconciliation_required or not session.source_complete:
                verified = session.verified_quote_volume
            return _session_projection(session, fills, verified)

    def account_summary(self, account_id: str, mode: str) -> dict[str, object]:
        with self._lock:
            fills = list(self._account_fills.get((account_id, mode), {}).values())
        return _fill_summary(fills)

    def fills_for_account(self, account_id: str, mode: str, started_at_ms: int = 0) -> tuple[NormalizedTradeFill, ...]:
        with self._lock:
            return tuple(
                fill
                for fill in self._account_fills.get((account_id, mode), {}).values()
                if fill.executed_at_ms >= started_at_ms
            )

    def save_sync_checkpoint(self, account_id: str, mode: str, **values: object) -> None:
        with self._lock:
            self._checkpoints[(account_id, mode)] = dict(values)

    def sync_checkpoint(self, account_id: str, mode: str) -> dict[str, object] | None:
        with self._lock:
            value = self._checkpoints.get((account_id, mode))
            return dict(value) if value is not None else None

    def refresh_sessions(self, account_id: str, mode: str, *, now_ms: int, source_complete: bool, stale: bool) -> None:
        with self._lock:
            for session_id, session in tuple(self._sessions.items()):
                if session.account_id != account_id or session.mode != mode:
                    continue
                fills = self._session_fills(session)
                verified = sum((fill.quote_volume for fill in fills if fill.authoritative), Decimal(0))
                updated = replace(
                    session,
                    verified_quote_volume=verified,
                    remaining_quote_volume=max(session.target_quote_volume - verified, Decimal(0)),
                    last_sync_at_ms=now_ms,
                    source_complete=source_complete,
                    stale=stale,
                    pending_sync=stale,
                )
                projected = _session_projection(updated, fills, verified)
                self._sessions[session_id] = replace(updated, status=str(projected["status"]))

    def latest_session(self, account_id: str, mode: str) -> dict[str, object] | None:
        with self._lock:
            sessions = [s for s in self._sessions.values() if s.account_id == account_id and s.mode == mode]
            if not sessions:
                return None
            return self.session_projection(max(sessions, key=lambda item: item.started_at_ms).session_id)

    def mark_sessions_reconciliation(self, account_id: str, mode: str, *, discrepancy: Decimal = Decimal(0)) -> None:
        with self._lock:
            for session_id, session in tuple(self._sessions.items()):
                if session.account_id == account_id and session.mode == mode:
                    self._sessions[session_id] = replace(
                        session,
                        reconciliation_required=True,
                        stale=True,
                        discrepancy_quote_volume=discrepancy,
                        pending_sync=True,
                    )


class SQLiteTradeVolumeLedger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS instances (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS trade_volume_fills (
                instance_id TEXT NOT NULL,
                identity TEXT NOT NULL,
                executed_at_ms INTEGER NOT NULL,
                quote_volume TEXT NOT NULL,
                symbol TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'legacy',
                order_id TEXT NOT NULL DEFAULT '',
                base_quantity TEXT NOT NULL DEFAULT '0',
                side TEXT NOT NULL DEFAULT '',
                position_side TEXT NOT NULL DEFAULT '',
                position_action TEXT NOT NULL DEFAULT 'unknown',
                maker INTEGER,
                commission TEXT NOT NULL DEFAULT '0',
                commission_asset TEXT,
                realized_pnl TEXT NOT NULL DEFAULT '0',
                source TEXT NOT NULL DEFAULT 'user_trades',
                authoritative INTEGER NOT NULL DEFAULT 1,
                created_at_ms INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(instance_id, identity),
                FOREIGN KEY(instance_id) REFERENCES instances(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_trade_volume_fills_instance_time
                ON trade_volume_fills(instance_id, executed_at_ms);
            CREATE TABLE IF NOT EXISTS trade_volume_totals (
                instance_id TEXT PRIMARY KEY,
                lifetime_quote TEXT NOT NULL DEFAULT '0',
                fill_count INTEGER NOT NULL DEFAULT 0,
                complete INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(instance_id) REFERENCES instances(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS volume_sessions (
                session_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                started_at_ms INTEGER NOT NULL,
                target_quote_volume TEXT NOT NULL,
                status TEXT NOT NULL,
                verified_quote_volume TEXT NOT NULL DEFAULT '0',
                remaining_quote_volume TEXT NOT NULL,
                last_sync_at_ms INTEGER,
                last_reconciliation_at_ms INTEGER,
                source_complete INTEGER NOT NULL DEFAULT 0,
                coverage_complete INTEGER NOT NULL DEFAULT 0,
                stale INTEGER NOT NULL DEFAULT 1,
                reconciliation_required INTEGER NOT NULL DEFAULT 0,
                discrepancy_quote_volume TEXT NOT NULL DEFAULT '0',
                cursor TEXT,
                high_watermark_ms INTEGER,
                pending_sync INTEGER NOT NULL DEFAULT 1,
                maker_only_required INTEGER NOT NULL DEFAULT 0,
                uncertain_order_state INTEGER NOT NULL DEFAULT 0,
                UNIQUE(account_id, mode, session_id)
            );
            CREATE INDEX IF NOT EXISTS idx_volume_sessions_account
                ON volume_sessions(account_id, mode, started_at_ms);
            CREATE TABLE IF NOT EXISTS volume_sync_checkpoints (
                account_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                cursor TEXT,
                high_watermark_ms INTEGER,
                pending_sync INTEGER NOT NULL DEFAULT 0,
                source_complete INTEGER NOT NULL DEFAULT 0,
                stale INTEGER NOT NULL DEFAULT 1,
                updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY(account_id, mode)
            );
            """
        )
        checkpoint_columns = {row[1] for row in self._connection.execute("PRAGMA table_info(volume_sync_checkpoints)")}
        if "coverage_complete" not in checkpoint_columns:
            self._connection.execute(
                "ALTER TABLE volume_sync_checkpoints ADD COLUMN coverage_complete INTEGER NOT NULL DEFAULT 0"
            )
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(trade_volume_fills)")}
        migrations = {
            "mode": "TEXT NOT NULL DEFAULT 'legacy'",
            "order_id": "TEXT NOT NULL DEFAULT ''",
            "base_quantity": "TEXT NOT NULL DEFAULT '0'",
            "side": "TEXT NOT NULL DEFAULT ''",
            "position_side": "TEXT NOT NULL DEFAULT ''",
            "position_action": "TEXT NOT NULL DEFAULT 'unknown'",
            "maker": "INTEGER",
            "commission": "TEXT NOT NULL DEFAULT '0'",
            "commission_asset": "TEXT",
            "realized_pnl": "TEXT NOT NULL DEFAULT '0'",
            "source": "TEXT NOT NULL DEFAULT 'user_trades'",
            "authoritative": "INTEGER NOT NULL DEFAULT 1",
            "created_at_ms": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in migrations.items():
            if name not in columns:
                self._connection.execute(f"ALTER TABLE trade_volume_fills ADD COLUMN {name} {definition}")
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_trade_volume_fills_account_mode_time "
            "ON trade_volume_fills(instance_id, mode, executed_at_ms)"
        )
        self._connection.commit()
        self._lock = RLock()

    def record(self, instance_id: str, fills: tuple[NormalizedTradeFill, ...]) -> int:
        inserted = 0
        inserted_volume = Decimal(0)
        with self._lock, self._connection:
            self._ensure_totals(instance_id)
            for fill in fills:
                row = self._connection.execute(
                    """
                        SELECT executed_at_ms, quote_volume, symbol
                        FROM trade_volume_fills
                        WHERE instance_id = ? AND identity = ?
                        """,
                    (instance_id, fill.identity),
                ).fetchone()
                if row is not None:
                    existing = (int(row[0]), Decimal(row[1]), str(row[2]))
                    expected = (fill.executed_at_ms, fill.quote_volume, fill.symbol)
                    if existing != expected:
                        raise FillConflictError(f"fill identity {fill.identity!r} changed across history pages")
                    continue
                self._connection.execute(
                    """
                        INSERT INTO trade_volume_fills(
                            instance_id, identity, executed_at_ms, quote_volume, symbol
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                    (
                        instance_id,
                        fill.identity,
                        fill.executed_at_ms,
                        str(fill.quote_volume),
                        fill.symbol,
                    ),
                )
                inserted += 1
                inserted_volume += fill.quote_volume
            if inserted:
                row = self._connection.execute(
                    "SELECT lifetime_quote, fill_count FROM trade_volume_totals WHERE instance_id = ?",
                    (instance_id,),
                ).fetchone()
                self._connection.execute(
                    """
                    UPDATE trade_volume_totals
                    SET lifetime_quote = ?, fill_count = ?
                    WHERE instance_id = ?
                    """,
                    (str(Decimal(row[0]) + inserted_volume), int(row[1]) + inserted, instance_id),
                )
        return inserted

    def set_complete(self, instance_id: str, complete: bool) -> None:
        with self._lock, self._connection:
            self._ensure_totals(instance_id)
            self._connection.execute(
                "UPDATE trade_volume_totals SET complete = ? WHERE instance_id = ?",
                (int(complete), instance_id),
            )

    def aggregate(self, instance_id: str, today_start_ms: int) -> TradeVolumeAggregate:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT lifetime_quote, fill_count, complete
                FROM trade_volume_totals
                WHERE instance_id = ?
                """,
                (instance_id,),
            ).fetchone()
            amounts = self._connection.execute(
                """
                SELECT quote_volume FROM trade_volume_fills
                WHERE instance_id = ? AND executed_at_ms >= ?
                """,
                (instance_id, today_start_ms),
            ).fetchall()
        if row is None:
            return TradeVolumeAggregate(Decimal(0), Decimal(0), 0, False)
        today = sum((Decimal(amount[0]) for amount in amounts), start=Decimal(0))
        return TradeVolumeAggregate(Decimal(row[0]), today, int(row[1]), bool(row[2]))

    def remove(self, instance_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM trade_volume_totals WHERE instance_id = ?", (instance_id,))
            self._connection.execute("DELETE FROM trade_volume_fills WHERE instance_id = ?", (instance_id,))

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def record_account_fills(self, account_id: str, mode: str, fills: tuple[NormalizedTradeFill, ...]) -> int:
        inserted = 0
        inserted_volume = Decimal(0)
        created_at_ms = int(datetime.now(UTC).timestamp() * 1000)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO instances(id, payload) VALUES (?, '{}') ON CONFLICT(id) DO NOTHING",
                (account_id,),
            )
            for fill in fills:
                identity = f"{mode}:{fill.identity}"
                row = self._connection.execute(
                    "SELECT executed_at_ms, quote_volume, symbol, mode, order_id, base_quantity, side, "
                    "position_side, position_action, maker, commission, commission_asset, realized_pnl, source, "
                    "authoritative "
                    "FROM trade_volume_fills WHERE instance_id = ? AND identity = ?",
                    (account_id, identity),
                ).fetchone()
                expected = (
                    fill.executed_at_ms,
                    str(fill.quote_volume),
                    fill.symbol,
                    mode,
                    fill.order_id,
                    str(fill.base_quantity),
                    fill.side,
                    fill.position_side,
                    fill.position_action,
                    None if fill.maker is None else int(fill.maker),
                    str(fill.commission),
                    fill.commission_asset,
                    str(fill.realized_pnl),
                    fill.source,
                    int(fill.authoritative),
                )
                if row is not None:
                    if tuple(row) != expected:
                        raise FillConflictError(f"fill identity {fill.identity!r} changed across history pages")
                    continue
                self._connection.execute(
                    """INSERT INTO trade_volume_fills(
                        instance_id, identity, executed_at_ms, quote_volume, symbol, mode, order_id,
                        base_quantity, side, position_side, position_action, maker, commission,
                        commission_asset, realized_pnl, source, authoritative, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        account_id,
                        identity,
                        fill.executed_at_ms,
                        str(fill.quote_volume),
                        fill.symbol,
                        *expected[3:],
                        fill.created_at_ms if fill.created_at_ms is not None else created_at_ms,
                    ),
                )
                inserted += 1
                inserted_volume += fill.quote_volume
            if inserted:
                self._ensure_totals(account_id)
                row = self._connection.execute(
                    "SELECT lifetime_quote, fill_count FROM trade_volume_totals WHERE instance_id = ?",
                    (account_id,),
                ).fetchone()
                self._connection.execute(
                    "UPDATE trade_volume_totals SET lifetime_quote = ?, fill_count = ? WHERE instance_id = ?",
                    (str(Decimal(str(row[0])) + inserted_volume), int(row[1]) + inserted, account_id),
                )
        return inserted

    def _row_to_session(self, row: sqlite3.Row) -> VolumeSession:
        return VolumeSession(
            session_id=str(row["session_id"]),
            account_id=str(row["account_id"]),
            mode=str(row["mode"]),
            started_at_ms=int(row["started_at_ms"]),
            target_quote_volume=Decimal(str(row["target_quote_volume"])),
            status=str(row["status"]),
            verified_quote_volume=Decimal(str(row["verified_quote_volume"])),
            remaining_quote_volume=Decimal(str(row["remaining_quote_volume"])),
            last_sync_at_ms=None if row["last_sync_at_ms"] is None else int(row["last_sync_at_ms"]),
            last_reconciliation_at_ms=None
            if row["last_reconciliation_at_ms"] is None
            else int(row["last_reconciliation_at_ms"]),
            source_complete=bool(row["source_complete"]),
            stale=bool(row["stale"]),
            reconciliation_required=bool(row["reconciliation_required"]),
            discrepancy_quote_volume=Decimal(str(row["discrepancy_quote_volume"])),
            cursor=row["cursor"],
            high_watermark_ms=row["high_watermark_ms"],
            pending_sync=bool(row["pending_sync"]),
            maker_only_required=bool(row["maker_only_required"]),
            uncertain_order_state=bool(row["uncertain_order_state"]),
        )

    def create_session(
        self,
        session_id: str,
        account_id: str,
        mode: str,
        started_at_ms: int,
        target_quote_volume: Decimal,
        *,
        maker_only_required: bool = False,
    ) -> VolumeSession:
        if started_at_ms < 0 or target_quote_volume <= 0 or not target_quote_volume.is_finite():
            raise ValueError("invalid volume session parameters")
        with self._lock, self._connection:
            active = self._connection.execute(
                "SELECT 1 FROM volume_sessions WHERE account_id = ? AND mode = ? AND status = 'running' LIMIT 1",
                (account_id, mode),
            ).fetchone()
            if active is not None:
                raise ValueError("an account can have only one running volume session")
            self._connection.execute(
                """INSERT INTO volume_sessions(
                    session_id, account_id, mode, started_at_ms, target_quote_volume, status,
                    verified_quote_volume, remaining_quote_volume, maker_only_required
                ) VALUES (?, ?, ?, ?, ?, 'running', '0', ?, ?)""",
                (
                    session_id,
                    account_id,
                    mode,
                    started_at_ms,
                    str(target_quote_volume),
                    str(target_quote_volume),
                    int(maker_only_required),
                ),
            )
        return self.get_session(session_id)  # type: ignore[return-value]

    def get_session(self, session_id: str) -> VolumeSession | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM volume_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return self._row_to_session(row) if row else None

    def update_session(self, session_id: str, **changes: object) -> VolumeSession:
        allowed = {
            "status",
            "verified_quote_volume",
            "remaining_quote_volume",
            "last_sync_at_ms",
            "last_reconciliation_at_ms",
            "source_complete",
            "stale",
            "reconciliation_required",
            "discrepancy_quote_volume",
            "cursor",
            "high_watermark_ms",
            "pending_sync",
            "uncertain_order_state",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown session fields: {sorted(unknown)}")
        current = self.get_session(session_id)
        if current is None:
            raise KeyError(session_id)
        if "verified_quote_volume" in changes and "remaining_quote_volume" not in changes:
            changes["remaining_quote_volume"] = max(
                current.target_quote_volume - Decimal(str(changes["verified_quote_volume"])), Decimal(0)
            )
        assignments = ", ".join(f"{key} = ?" for key in changes)
        values = [
            int(value)
            if key in {"source_complete", "stale", "reconciliation_required", "pending_sync", "uncertain_order_state"}
            else str(value)
            if key in {"verified_quote_volume", "remaining_quote_volume", "discrepancy_quote_volume"}
            else value
            for key, value in changes.items()
        ]
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE volume_sessions SET {assignments} WHERE session_id = ?", (*values, session_id)
            )
        return self.get_session(session_id)  # type: ignore[return-value]

    def _session_fills(self, session: VolumeSession) -> list[NormalizedTradeFill]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT identity, executed_at_ms, quote_volume, symbol, order_id, base_quantity, side,
                   position_side, position_action, maker, commission, commission_asset, realized_pnl,
                   source, authoritative, created_at_ms FROM trade_volume_fills
                   WHERE instance_id = ? AND mode = ? AND executed_at_ms >= ?""",
                (session.account_id, session.mode, session.started_at_ms),
            ).fetchall()
        return [
            NormalizedTradeFill(
                identity=str(row[0]).split(":", 1)[-1],
                executed_at_ms=int(row[1]),
                quote_volume=Decimal(str(row[2])),
                symbol=str(row[3]),
                order_id=str(row[4]),
                base_quantity=Decimal(str(row[5])),
                side=str(row[6]),
                position_side=str(row[7]),
                position_action=str(row[8]),
                maker=None if row[9] is None else bool(row[9]),
                commission=Decimal(str(row[10])),
                commission_asset=row[11],
                realized_pnl=Decimal(str(row[12])),
                source=str(row[13]),
                authoritative=bool(row[14]),
                created_at_ms=int(row[15]),
            )
            for row in rows
        ]

    def session_projection(self, session_id: str) -> dict[str, object]:
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        fills = self._session_fills(session)
        verified = sum((fill.quote_volume for fill in fills if fill.authoritative), Decimal(0))
        if session.stale or session.reconciliation_required or not session.source_complete:
            verified = session.verified_quote_volume
        return _session_projection(session, fills, verified)

    def account_summary(self, account_id: str, mode: str) -> dict[str, object]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT identity, executed_at_ms, quote_volume, symbol, order_id, base_quantity, side,
                   position_side, position_action, maker, commission, commission_asset, realized_pnl,
                   source, authoritative, created_at_ms FROM trade_volume_fills
                   WHERE instance_id = ? AND mode = ?""",
                (account_id, mode),
            ).fetchall()
        fills = [
            NormalizedTradeFill(
                identity=str(row[0]).split(":", 1)[-1],
                executed_at_ms=int(row[1]),
                quote_volume=Decimal(str(row[2])),
                symbol=str(row[3]),
                order_id=str(row[4]),
                base_quantity=Decimal(str(row[5])),
                side=str(row[6]),
                position_side=str(row[7]),
                position_action=str(row[8]),
                maker=None if row[9] is None else bool(row[9]),
                commission=Decimal(str(row[10])),
                commission_asset=row[11],
                realized_pnl=Decimal(str(row[12])),
                source=str(row[13]),
                authoritative=bool(row[14]),
                created_at_ms=int(row[15]),
            )
            for row in rows
        ]
        return _fill_summary(fills)

    def fills_for_account(self, account_id: str, mode: str, started_at_ms: int = 0) -> tuple[NormalizedTradeFill, ...]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT identity, executed_at_ms, quote_volume, symbol, order_id, base_quantity, side,
                   position_side, position_action, maker, commission, commission_asset, realized_pnl,
                   source, authoritative, created_at_ms FROM trade_volume_fills
                   WHERE instance_id = ? AND mode = ? AND executed_at_ms >= ?""",
                (account_id, mode, started_at_ms),
            ).fetchall()
        return tuple(
            NormalizedTradeFill(
                identity=str(row[0]).split(":", 1)[-1],
                executed_at_ms=int(row[1]),
                quote_volume=Decimal(str(row[2])),
                symbol=str(row[3]),
                order_id=str(row[4]),
                base_quantity=Decimal(str(row[5])),
                side=str(row[6]),
                position_side=str(row[7]),
                position_action=str(row[8]),
                maker=None if row[9] is None else bool(row[9]),
                commission=Decimal(str(row[10])),
                commission_asset=row[11],
                realized_pnl=Decimal(str(row[12])),
                source=str(row[13]),
                authoritative=bool(row[14]),
                created_at_ms=int(row[15]),
            )
            for row in rows
        )

    def save_sync_checkpoint(self, account_id: str, mode: str, **values: object) -> None:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO volume_sync_checkpoints(
                    account_id, mode, cursor, high_watermark_ms, pending_sync, source_complete,
                    coverage_complete, stale, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, mode) DO UPDATE SET cursor=excluded.cursor,
                    high_watermark_ms=excluded.high_watermark_ms, pending_sync=excluded.pending_sync,
                    source_complete=excluded.source_complete, coverage_complete=excluded.coverage_complete,
                    stale=excluded.stale,
                    updated_at_ms=excluded.updated_at_ms""",
                (
                    account_id,
                    mode,
                    values.get("cursor"),
                    values.get("high_watermark_ms"),
                    int(bool(values.get("pending", values.get("pending_sync", False)))),
                    int(bool(values.get("source_complete", False))),
                    int(bool(values.get("coverage_complete", values.get("source_complete", False)))),
                    int(bool(values.get("stale", True))),
                    now_ms,
                ),
            )

    def sync_checkpoint(self, account_id: str, mode: str) -> dict[str, object] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT cursor, high_watermark_ms, pending_sync, source_complete, coverage_complete, stale, "
                "updated_at_ms "
                "FROM volume_sync_checkpoints WHERE account_id = ? AND mode = ?",
                (account_id, mode),
            ).fetchone()
        if row is None:
            return None
        return {
            "cursor": row[0],
            "high_watermark_ms": row[1],
            "pending": bool(row[2]),
            "source_complete": bool(row[3]),
            "coverage_complete": bool(row[4]),
            "stale": bool(row[5]),
            "updated_at_ms": row[6],
        }

    def refresh_sessions(self, account_id: str, mode: str, *, now_ms: int, source_complete: bool, stale: bool) -> None:
        with self._lock:
            rows = self._connection.execute(
                "SELECT session_id FROM volume_sessions WHERE account_id = ? AND mode = ?", (account_id, mode)
            ).fetchall()
        for row in rows:
            session = self.get_session(str(row[0]))
            assert session is not None
            fills = self.fills_for_account(account_id, mode, session.started_at_ms)
            verified = sum((fill.quote_volume for fill in fills if fill.authoritative), Decimal(0))
            self.update_session(
                session.session_id,
                verified_quote_volume=verified,
                last_sync_at_ms=now_ms,
                source_complete=source_complete,
                stale=stale,
                pending_sync=stale,
            )
            projected = self.session_projection(session.session_id)
            if projected["status"] == "completed":
                self.update_session(session.session_id, status="completed")

    def latest_session(self, account_id: str, mode: str) -> dict[str, object] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT session_id FROM volume_sessions WHERE account_id = ? AND mode = ? "
                "ORDER BY started_at_ms DESC LIMIT 1",
                (account_id, mode),
            ).fetchone()
        return self.session_projection(str(row[0])) if row else None

    def mark_sessions_reconciliation(self, account_id: str, mode: str, *, discrepancy: Decimal = Decimal(0)) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE volume_sessions SET reconciliation_required = 1, stale = 1,
                   pending_sync = 1, discrepancy_quote_volume = ?
                   WHERE account_id = ? AND mode = ?""",
                (str(discrepancy), account_id, mode),
            )

    def _ensure_totals(self, instance_id: str) -> None:
        self._connection.execute(
            """
            INSERT INTO trade_volume_totals(instance_id, lifetime_quote, fill_count, complete)
            VALUES (?, '0', 0, 0)
            ON CONFLICT(instance_id) DO NOTHING
            """,
            (instance_id,),
        )


class TradeHistorySynchronizer:
    def __init__(self, ledger: TradeVolumeLedger, *, page_size: int = 100, max_pages: int = 1000) -> None:
        if page_size < 1:
            raise ValueError("history page size must be at least 1")
        if max_pages < 1:
            raise ValueError("history max pages must be at least 1")
        self._ledger = ledger
        self._page_size = page_size
        self._max_pages = max_pages

    async def sync(
        self,
        instance_id: str,
        context: TradeHistoryContext,
        source: TradeHistorySource,
        *,
        today_start_ms: int,
        cursor: str | None = None,
    ) -> TradeHistorySyncResult:
        if cursor is None:
            self._ledger.set_complete(instance_id, False)
        seen_cursors: set[str] = set()
        inserted = 0
        high_watermark_ms: int | None = None
        account_mode = getattr(context.instance.mode, "value", str(context.instance.mode)).lower()
        for page_number in range(1, self._max_pages + 1):
            try:
                page = await source.fetch_page(context, cursor=cursor, limit=self._page_size)
            except Exception:
                checkpoint = self._ledger.sync_checkpoint(instance_id, account_mode) or {}
                self._ledger.save_sync_checkpoint(
                    instance_id,
                    account_mode,
                    cursor=cursor,
                    high_watermark_ms=high_watermark_ms,
                    pending=True,
                    source_complete=False,
                    coverage_complete=bool(checkpoint.get("coverage_complete", False)),
                    stale=True,
                )
                self._ledger.refresh_sessions(
                    instance_id,
                    account_mode,
                    now_ms=int(datetime.now(UTC).timestamp() * 1000),
                    source_complete=False,
                    stale=True,
                )
                raise
            candidates = [value for value in (high_watermark_ms, page.high_watermark_ms) if value is not None]
            high_watermark_ms = max(candidates) if candidates else None
            if hasattr(self._ledger, "record_account_fills"):
                try:
                    inserted += self._ledger.record_account_fills(instance_id, account_mode, page.fills)
                except FillConflictError:
                    if hasattr(self._ledger, "mark_sessions_reconciliation"):
                        self._ledger.mark_sessions_reconciliation(instance_id, account_mode)
                    raise
            else:
                inserted += self._ledger.record(instance_id, page.fills)
            if page.next_cursor is None:
                if page.complete:
                    self._ledger.set_complete(instance_id, True)
                if hasattr(self._ledger, "save_sync_checkpoint"):
                    self._ledger.save_sync_checkpoint(
                        instance_id,
                        account_mode,
                        cursor=None,
                        high_watermark_ms=high_watermark_ms,
                        pending=False,
                        source_complete=page.complete,
                        coverage_complete=page.complete,
                        stale=not page.complete,
                    )
                if hasattr(self._ledger, "refresh_sessions"):
                    self._ledger.refresh_sessions(
                        instance_id,
                        account_mode,
                        now_ms=int(datetime.now(UTC).timestamp() * 1000),
                        source_complete=page.complete,
                        stale=not page.complete,
                    )
                return TradeHistorySyncResult(
                    aggregate=self._ledger.aggregate(instance_id, today_start_ms),
                    pages_fetched=page_number,
                    fills_inserted=inserted,
                    stop_reason="history_exhausted" if page.complete else "source_incomplete",
                    next_cursor=None,
                )
            if page.next_cursor == cursor or page.next_cursor in seen_cursors:
                if hasattr(self._ledger, "save_sync_checkpoint"):
                    self._ledger.save_sync_checkpoint(
                        instance_id,
                        account_mode,
                        cursor=cursor,
                        high_watermark_ms=high_watermark_ms,
                        pending=True,
                        source_complete=False,
                        coverage_complete=page.complete,
                        stale=True,
                    )
                if hasattr(self._ledger, "refresh_sessions"):
                    self._ledger.refresh_sessions(
                        instance_id,
                        account_mode,
                        now_ms=int(datetime.now(UTC).timestamp() * 1000),
                        source_complete=False,
                        stale=True,
                    )
                return TradeHistorySyncResult(
                    aggregate=self._ledger.aggregate(instance_id, today_start_ms),
                    pages_fetched=page_number,
                    fills_inserted=inserted,
                    stop_reason="cursor_loop",
                    next_cursor=cursor,
                )
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        if hasattr(self._ledger, "save_sync_checkpoint"):
            self._ledger.save_sync_checkpoint(
                instance_id,
                account_mode,
                cursor=cursor,
                high_watermark_ms=high_watermark_ms,
                pending=True,
                source_complete=False,
                coverage_complete=page.complete,
                stale=True,
            )
        if hasattr(self._ledger, "refresh_sessions"):
            self._ledger.refresh_sessions(
                instance_id,
                account_mode,
                now_ms=int(datetime.now(UTC).timestamp() * 1000),
                source_complete=False,
                stale=True,
            )
        return TradeHistorySyncResult(
            aggregate=self._ledger.aggregate(instance_id, today_start_ms),
            pages_fetched=self._max_pages,
            fills_inserted=inserted,
            stop_reason="page_budget_exhausted",
            next_cursor=cursor,
        )


class SessionVolumeService:
    """Small application boundary for session progress and explicit reconciliation."""

    def __init__(self, ledger: TradeVolumeLedger) -> None:
        self.ledger = ledger

    def start(
        self,
        *,
        session_id: str,
        account_id: str,
        mode: str,
        started_at_ms: int,
        target_quote_volume: Decimal,
        maker_only_required: bool = False,
    ) -> dict[str, object]:
        session = self.ledger.create_session(
            session_id,
            account_id,
            mode,
            started_at_ms,
            target_quote_volume,
            maker_only_required=maker_only_required,
        )
        return self.ledger.session_projection(session.session_id)

    def progress(self, session_id: str) -> dict[str, object]:
        return self.ledger.session_projection(session_id)

    def reconcile(
        self,
        session_id: str,
        authoritative_fills: tuple[NormalizedTradeFill, ...],
        *,
        reconciled_at_ms: int,
    ) -> dict[str, object]:
        session = self.ledger.get_session(session_id)
        if session is None:
            raise KeyError(session_id)
        expected = {fill.identity: fill for fill in authoritative_fills if fill.executed_at_ms >= session.started_at_ms}
        existing = {
            fill.identity: fill
            for fill in getattr(self.ledger, "fills_for_account", lambda *_: ())(
                session.account_id, session.mode, session.started_at_ms
            )
        }
        missing = set(expected) - set(existing)
        extra = set(existing) - set(expected)
        shared = set(expected) & set(existing)
        changed = {key for key in shared if _fill_signature(expected[key]) != _fill_signature(existing[key])}
        discrepancy = (
            sum((expected[key].quote_volume for key in missing), Decimal(0))
            + sum((existing[key].quote_volume for key in extra), Decimal(0))
            + sum(
                (abs(expected[key].quote_volume - existing[key].quote_volume) for key in changed),
                Decimal(0),
            )
        )
        if missing or extra or changed:
            self.ledger.update_session(
                session_id,
                reconciliation_required=True,
                stale=True,
                discrepancy_quote_volume=discrepancy,
                last_reconciliation_at_ms=reconciled_at_ms,
                pending_sync=False,
            )
        else:
            self.ledger.update_session(
                session_id,
                reconciliation_required=False,
                stale=False,
                source_complete=True,
                discrepancy_quote_volume=Decimal(0),
                last_reconciliation_at_ms=reconciled_at_ms,
                pending_sync=False,
            )
        return self.ledger.session_projection(session_id)


def utc_day_start_ms(now_ms: int) -> int:
    if now_ms < 0:
        raise ValueError("timestamp cannot be negative")
    instant = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    start = instant.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def shanghai_day_start_ms(now_ms: int) -> int:
    if now_ms < 0:
        raise ValueError("timestamp cannot be negative")
    instant = datetime.fromtimestamp(now_ms / 1000, tz=SHANGHAI)
    start = instant.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def _aggregate(
    fills: tuple[NormalizedTradeFill, ...],
    today_start_ms: int,
    complete: bool,
) -> TradeVolumeAggregate:
    lifetime = sum((fill.quote_volume for fill in fills), start=Decimal(0))
    today = sum(
        (fill.quote_volume for fill in fills if fill.executed_at_ms >= today_start_ms),
        start=Decimal(0),
    )
    return TradeVolumeAggregate(lifetime, today, len(fills), complete)


def _fill_signature(fill: NormalizedTradeFill) -> tuple[object, ...]:
    return (
        fill.identity,
        fill.executed_at_ms,
        fill.quote_volume,
        fill.symbol,
        fill.order_id,
        fill.base_quantity,
        fill.side,
        fill.position_side,
        fill.position_action,
        fill.maker,
        fill.commission,
        fill.commission_asset,
        fill.realized_pnl,
        fill.source,
        fill.authoritative,
    )


def _fill_summary(fills: list[NormalizedTradeFill]) -> dict[str, object]:
    opening = sum((f.quote_volume for f in fills if f.position_action == "open"), Decimal(0))
    closing = sum((f.quote_volume for f in fills if f.position_action == "close"), Decimal(0))
    maker = sum((f.quote_volume for f in fills if f.maker is True), Decimal(0))
    taker = sum((f.quote_volume for f in fills if f.maker is False), Decimal(0))
    unknown = sum((f.quote_volume for f in fills if f.maker is None), Decimal(0))
    return {
        "fill_count": len(fills),
        "total_quote_volume": str(sum((f.quote_volume for f in fills), Decimal(0))),
        "opening_quote_volume": str(opening),
        "closing_quote_volume": str(closing),
        "maker_quote_volume": str(maker),
        "taker_quote_volume": str(taker),
        "unknown_liquidity_quote_volume": str(unknown),
        "authoritative_fill_count": sum(1 for f in fills if f.authoritative),
    }


def _session_projection(
    session: VolumeSession,
    fills: list[NormalizedTradeFill],
    verified: Decimal,
) -> dict[str, object]:
    summary = _fill_summary(fills)
    remaining = max(session.target_quote_volume - verified, Decimal(0))
    eligible_maker = all(f.maker is True for f in fills if f.authoritative) if fills else True
    complete = (
        remaining <= 0
        and session.source_complete
        and not session.stale
        and not session.reconciliation_required
        and not session.pending_sync
        and not session.uncertain_order_state
        and (not session.maker_only_required or eligible_maker)
    )
    status = "completed" if complete else session.status
    if session.stale or session.reconciliation_required or not session.source_complete:
        status = "stale" if session.stale or not session.source_complete else "uncertain"
    return {
        **session.as_dict(),
        **summary,
        "verified_quote_volume": str(verified),
        "remaining_quote_volume": str(remaining),
        "status": status,
        "maker_only_verified": eligible_maker,
        "retry_allowed": False,
    }

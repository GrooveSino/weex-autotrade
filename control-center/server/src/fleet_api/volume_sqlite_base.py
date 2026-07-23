from __future__ import annotations

import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import RLock

from .volume_contracts import *  # noqa: F403
from .volume_helpers import _aggregate, _fill_signature, _fill_summary, _normalized_session_status, _session_projection


class SQLiteLedgerBase:
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
                strategy_id TEXT,
                strategy_name TEXT,
                strategy_version INTEGER,
                target_mode TEXT NOT NULL DEFAULT 'incremental',
                strategy_target_quote_volume TEXT,
                baseline_lifetime_quote_volume TEXT NOT NULL DEFAULT '0',
                finished_at_ms INTEGER,
                result TEXT,
                result_reason TEXT,
                final_lifetime_quote_volume TEXT,
                starting_available_balance_quote TEXT,
                ending_available_balance_quote TEXT,
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
        session_columns = {row[1] for row in self._connection.execute("PRAGMA table_info(volume_sessions)")}
        session_migrations = {
            "strategy_id": "TEXT",
            "strategy_name": "TEXT",
            "strategy_version": "INTEGER",
            "target_mode": "TEXT NOT NULL DEFAULT 'incremental'",
            "strategy_target_quote_volume": "TEXT",
            "baseline_lifetime_quote_volume": "TEXT NOT NULL DEFAULT '0'",
            "finished_at_ms": "INTEGER",
            "result": "TEXT",
            "result_reason": "TEXT",
            "final_lifetime_quote_volume": "TEXT",
            "starting_available_balance_quote": "TEXT",
            "ending_available_balance_quote": "TEXT",
        }
        for name, definition in session_migrations.items():
            if name not in session_columns:
                self._connection.execute(f"ALTER TABLE volume_sessions ADD COLUMN {name} {definition}")
        self._connection.execute("UPDATE volume_sessions SET status = 'active' WHERE status = 'running'")
        self._connection.execute(
            "UPDATE volume_sessions SET status = 'verification_pending' WHERE status = 'stale'"
        )
        self._connection.execute(
            "UPDATE volume_sessions SET strategy_target_quote_volume = target_quote_volume "
            "WHERE strategy_target_quote_volume IS NULL"
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_trade_volume_fills_account_mode_time "
            "ON trade_volume_fills(instance_id, mode, executed_at_ms)"
        )
        self._connection.commit()
        self._lock = RLock()

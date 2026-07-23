from __future__ import annotations

import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import RLock

from .volume_contracts import *  # noqa: F403
from .volume_helpers import _aggregate, _fill_signature, _fill_summary, _normalized_session_status, _session_projection


class SQLiteLedgerSyncMixin:
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

    def refresh_sessions(
        self,
        account_id: str,
        mode: str,
        *,
        now_ms: int,
        source_complete: bool,
        stale: bool,
        coverage_start_ms: int | None = None,
        high_watermark_ms: int | None = None,
    ) -> None:
        with self._lock:
            rows = self._connection.execute(
                "SELECT session_id FROM volume_sessions WHERE account_id = ? AND mode = ? "
                "AND status NOT IN ('completed', 'stopped')",
                (account_id, mode),
            ).fetchall()
        for row in rows:
            session = self.get_session(str(row[0]))
            assert session is not None
            fills = self.fills_for_account(account_id, mode, session.started_at_ms)
            verified = sum((fill.quote_volume for fill in fills if fill.authoritative), Decimal(0))
            session_window_complete = source_complete and (
                session.source_complete
                or coverage_start_ms is None
                or coverage_start_ms <= session.started_at_ms
            )
            self.update_session(
                session.session_id,
                verified_quote_volume=verified,
                last_sync_at_ms=now_ms,
                source_complete=session_window_complete,
                stale=stale or not session_window_complete,
                pending_sync=stale or not session_window_complete,
                high_watermark_ms=high_watermark_ms or session.high_watermark_ms,
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

    def active_session(self, account_id: str, mode: str) -> dict[str, object] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT session_id FROM volume_sessions WHERE account_id = ? AND mode = ? "
                "AND status IN ('active', 'stopping', 'verification_pending', 'uncertain') "
                "ORDER BY started_at_ms DESC, session_id DESC LIMIT 1",
                (account_id, mode),
            ).fetchone()
        return self.session_projection(str(row[0])) if row else None

    def latest_terminal_session(self, account_id: str, mode: str) -> dict[str, object] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT session_id FROM volume_sessions WHERE account_id = ? AND mode = ? "
                "AND status IN ('completed', 'stopped') "
                "ORDER BY started_at_ms DESC, session_id DESC LIMIT 1",
                (account_id, mode),
            ).fetchone()
        return self.session_projection(str(row[0])) if row else None

    def list_sessions(
        self, account_id: str, mode: str, *, limit: int, cursor: str | None = None
    ) -> tuple[list[dict[str, object]], str | None]:
        if limit < 1:
            raise ValueError("session history limit must be positive")
        boundary: tuple[int, str] | None = None
        with self._lock:
            if cursor is not None:
                row = self._connection.execute(
                    "SELECT started_at_ms, session_id FROM volume_sessions WHERE session_id = ? "
                    "AND account_id = ? AND mode = ?",
                    (cursor, account_id, mode),
                ).fetchone()
                if row is None:
                    return [], None
                boundary = (int(row[0]), str(row[1]))
            query = (
                "SELECT session_id FROM volume_sessions WHERE account_id = ? AND mode = ?"
            )
            parameters: list[object] = [account_id, mode]
            if boundary is not None:
                query += " AND (started_at_ms < ? OR (started_at_ms = ? AND session_id < ?))"
                parameters.extend((boundary[0], boundary[0], boundary[1]))
            query += " ORDER BY started_at_ms DESC, session_id DESC LIMIT ?"
            parameters.append(limit + 1)
            rows = self._connection.execute(query, parameters).fetchall()
        selected = [str(row[0]) for row in rows[:limit]]
        next_cursor = selected[-1] if len(rows) > limit and selected else None
        return [self.session_projection(session_id) for session_id in selected], next_cursor

    def mark_sessions_reconciliation(self, account_id: str, mode: str, *, discrepancy: Decimal = Decimal(0)) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE volume_sessions SET status = 'verification_pending', reconciliation_required = 1, stale = 1,
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

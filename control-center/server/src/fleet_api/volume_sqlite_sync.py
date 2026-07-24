from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal


class SQLiteLedgerSyncMixin:
    def save_sync_checkpoint(self, account_id: str, mode: str, **values: object) -> None:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT cursor, high_watermark_ms, pending_sync, source_complete, coverage_complete, stale, "
                "scan_state_json, sync_reason, next_sync_at_ms, last_success_at_ms, initial_baseline_state "
                "FROM volume_sync_checkpoints WHERE account_id = ? AND mode = ?",
                (account_id, mode),
            ).fetchone()
            previous = dict(existing) if existing is not None else {}
            scan_state = (
                values["scan_state"] if "scan_state" in values else _decode_state(previous.get("scan_state_json"))
            )
            self._connection.execute(
                """INSERT INTO volume_sync_checkpoints(
                    account_id, mode, cursor, high_watermark_ms, pending_sync, source_complete,
                    coverage_complete, stale, scan_state_json, sync_reason, next_sync_at_ms,
                    last_success_at_ms, initial_baseline_state, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, mode) DO UPDATE SET cursor=excluded.cursor,
                    high_watermark_ms=excluded.high_watermark_ms, pending_sync=excluded.pending_sync,
                    source_complete=excluded.source_complete, coverage_complete=excluded.coverage_complete,
                    stale=excluded.stale, scan_state_json=excluded.scan_state_json,
                    sync_reason=excluded.sync_reason, next_sync_at_ms=excluded.next_sync_at_ms,
                    last_success_at_ms=excluded.last_success_at_ms,
                    initial_baseline_state=excluded.initial_baseline_state,
                    updated_at_ms=excluded.updated_at_ms""",
                (
                    account_id,
                    mode,
                    values.get("cursor", previous.get("cursor")),
                    values.get("high_watermark_ms", previous.get("high_watermark_ms")),
                    int(bool(values.get("pending", values.get("pending_sync", previous.get("pending_sync", False))))),
                    int(bool(values.get("source_complete", previous.get("source_complete", False)))),
                    int(
                        bool(
                            values.get(
                                "coverage_complete",
                                values.get("source_complete", previous.get("coverage_complete", False)),
                            )
                        )
                    ),
                    int(bool(values.get("stale", previous.get("stale", True)))),
                    json.dumps(scan_state, separators=(",", ":")) if scan_state is not None else None,
                    values.get("sync_reason", previous.get("sync_reason")),
                    values.get("next_sync_at_ms", previous.get("next_sync_at_ms")),
                    values.get("last_success_at_ms", previous.get("last_success_at_ms")),
                    values.get("initial_baseline_state", previous.get("initial_baseline_state", "not_requested")),
                    now_ms,
                ),
            )

    def sync_checkpoint(self, account_id: str, mode: str) -> dict[str, object] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT cursor, high_watermark_ms, pending_sync, source_complete, coverage_complete, stale, "
                "scan_state_json, sync_reason, next_sync_at_ms, last_success_at_ms, "
                "initial_baseline_state, updated_at_ms "
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
            "scan_state": _decode_state(row[6]),
            "sync_reason": row[7],
            "next_sync_at_ms": row[8],
            "last_success_at_ms": row[9],
            "initial_baseline_state": row[10],
            "updated_at_ms": row[11],
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
                session.source_complete or coverage_start_ms is None or coverage_start_ms <= session.started_at_ms
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
                "AND status IN ('active', 'recovering', 'stopping') "
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
            query = "SELECT session_id FROM volume_sessions WHERE account_id = ? AND mode = ?"
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
                """UPDATE volume_sessions SET audit_status = 'discrepant', reconciliation_required = 1, stale = 1,
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


def _decode_state(value: object) -> object | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None

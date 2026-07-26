from __future__ import annotations

import json
import time
from typing import Any

from weex_cli.control_api.progress import ExecutionProgressProjector

from fleet_api.campaigns.core.campaign_contracts import ExecutionMonitorProjection
from fleet_api.campaigns.persistence.json_codec import compact_json
from fleet_api.services.control.service import UnsafeOperation


class SQLiteCampaignJournalMonitorMixin:
    def add_event(self, campaign_id: str, event: dict[str, Any]) -> int:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                sequence = self._reserve_sequence(campaign_id.lower())
                stored_event = {**event, "sequence": sequence}
                self._connection.execute(
                    "INSERT INTO beta_campaign_events(campaign_id, sequence, payload, created_at_ms) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        campaign_id.lower(),
                        sequence,
                        compact_json(stored_event),
                        int(time.time() * 1000),
                    ),
                )
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
                return sequence

    def append_and_project(
        self,
        campaign_id: str,
        event: dict[str, Any],
        *,
        owner_user_id: str,
        account_id: str,
        session_id: str | None,
        executor_generation: str,
        projection_version: int,
        state: dict[str, Any] | None = None,
    ) -> int:
        normalized_campaign_id = campaign_id.lower()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                campaign_row = self._connection.execute(
                    "SELECT instance_id, metadata_json FROM beta_campaigns WHERE campaign_id = ?",
                    (normalized_campaign_id,),
                ).fetchone()
                if campaign_row is None:
                    raise KeyError(campaign_id)
                if str(campaign_row[0]) != account_id:
                    raise UnsafeOperation("execution monitor account mismatch")
                existing = self._connection.execute(
                    "SELECT owner_user_id, state_json FROM execution_monitor_projections WHERE execution_id = ?",
                    (normalized_campaign_id,),
                ).fetchone()
                if existing is not None and str(existing[0]) != owner_user_id:
                    raise UnsafeOperation("execution monitor owner mismatch")
                sequence = self._reserve_sequence(normalized_campaign_id)
                stored_event = {**event, "sequence": sequence}
                event_json = compact_json(stored_event)
                projected_state = state
                if projected_state is None:
                    projector = ExecutionProgressProjector.from_snapshot(
                        json.loads(str(existing[1])) if existing is not None else None
                    )
                    projector.apply(stored_event, at_ms=int(stored_event.get("at_ms") or 0))
                    projected_state = projector.snapshot()
                state_json = compact_json(projected_state)
                now_ms = int(event.get("at_ms") or time.time() * 1000)
                self._connection.execute(
                    "INSERT INTO beta_campaign_events(campaign_id, sequence, payload, created_at_ms) "
                    "VALUES (?, ?, ?, ?)",
                    (normalized_campaign_id, sequence, event_json, now_ms),
                )
                self._connection.execute(
                    """INSERT INTO execution_monitor_projections(
                        owner_user_id, account_id, execution_id, session_id, executor_generation,
                        projected_sequence, projection_version, state_json, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(execution_id) DO UPDATE SET
                        owner_user_id=excluded.owner_user_id,
                        account_id=excluded.account_id,
                        session_id=excluded.session_id,
                        executor_generation=excluded.executor_generation,
                        projected_sequence=excluded.projected_sequence,
                        projection_version=excluded.projection_version,
                        state_json=excluded.state_json,
                        updated_at_ms=excluded.updated_at_ms""",
                    (
                        owner_user_id,
                        account_id,
                        normalized_campaign_id,
                        session_id,
                        executor_generation,
                        sequence,
                        projection_version,
                        state_json,
                        now_ms,
                    ),
                )
                metadata = {
                    **json.loads(str(campaign_row[1])),
                    "monitor_state": projected_state,
                    "phase": projected_state.get("phase"),
                    "current_run": projected_state.get("current_run"),
                }
                self._connection.execute(
                    "UPDATE beta_campaigns SET metadata_json = ?, updated_at_ms = ? WHERE campaign_id = ?",
                    (compact_json(metadata), now_ms, normalized_campaign_id),
                )
            except Exception:
                self._connection.rollback()
                self._monitor_transaction_failures += 1
                raise
            else:
                self._connection.commit()
                return sequence

    def monitor_projection(self, campaign_id: str) -> ExecutionMonitorProjection | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT owner_user_id, account_id, execution_id, session_id, executor_generation, "
                "projected_sequence, projection_version, state_json, updated_at_ms "
                "FROM execution_monitor_projections WHERE execution_id = ?",
                (campaign_id.lower(),),
            ).fetchone()
        return self._projection(row) if row else None

    def _reserve_sequence(self, campaign_id: str) -> int:
        self._connection.execute(
            "UPDATE campaign_event_sequences SET next_sequence = next_sequence + 1 WHERE campaign_id = ?",
            (campaign_id,),
        )
        row = self._connection.execute(
            "SELECT next_sequence FROM campaign_event_sequences WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
        if row is None:
            raise KeyError(campaign_id)
        return int(row[0]) - 1

    def replace_monitor_projection(self, projection: ExecutionMonitorProjection) -> None:
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT owner_user_id FROM execution_monitor_projections WHERE execution_id = ?",
                (projection.execution_id.lower(),),
            ).fetchone()
            if existing is not None and str(existing[0]) != projection.owner_user_id:
                raise UnsafeOperation("execution monitor owner mismatch")
            sequence_row = self._connection.execute(
                "SELECT projected_sequence FROM execution_monitor_projections WHERE execution_id = ?",
                (projection.execution_id.lower(),),
            ).fetchone()
            if sequence_row is not None and int(sequence_row[0]) > projection.projected_sequence:
                return
            self._connection.execute(
                """INSERT INTO execution_monitor_projections(
                    owner_user_id, account_id, execution_id, session_id, executor_generation,
                    projected_sequence, projection_version, state_json, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_id) DO UPDATE SET
                    owner_user_id=excluded.owner_user_id,
                    account_id=excluded.account_id,
                    session_id=excluded.session_id,
                    executor_generation=excluded.executor_generation,
                    projected_sequence=excluded.projected_sequence,
                    projection_version=excluded.projection_version,
                    state_json=excluded.state_json,
                    updated_at_ms=excluded.updated_at_ms""",
                (
                    projection.owner_user_id,
                    projection.account_id,
                    projection.execution_id.lower(),
                    projection.session_id,
                    projection.executor_generation,
                    projection.projected_sequence,
                    projection.projection_version,
                    compact_json(projection.state),
                    projection.updated_at_ms,
                ),
            )

    def monitor_read(
        self, campaign_id: str, before_sequence: int | None, limit: int
    ) -> tuple[ExecutionMonitorProjection | None, list[dict[str, Any]], int]:
        normalized_campaign_id = campaign_id.lower()
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                projection_row = self._connection.execute(
                    "SELECT owner_user_id, account_id, execution_id, session_id, executor_generation, "
                    "projected_sequence, projection_version, state_json, updated_at_ms "
                    "FROM execution_monitor_projections WHERE execution_id = ?",
                    (normalized_campaign_id,),
                ).fetchone()
                latest_row = self._connection.execute(
                    "SELECT COALESCE(next_sequence - 1, 0) FROM campaign_event_sequences WHERE campaign_id = ?",
                    (normalized_campaign_id,),
                ).fetchone()
                query = "SELECT payload FROM beta_campaign_events WHERE campaign_id = ?"
                parameters: list[object] = [normalized_campaign_id]
                if before_sequence is not None:
                    query += " AND sequence < ?"
                    parameters.append(before_sequence)
                query += " ORDER BY sequence DESC LIMIT ?"
                parameters.append(limit)
                rows = self._connection.execute(query, parameters).fetchall()
            finally:
                self._connection.rollback()
        projection = self._projection(projection_row) if projection_row else None
        return projection, [json.loads(str(row[0])) for row in reversed(rows)], int(latest_row[0])

    def monitor_metrics(self) -> dict[str, int | None]:
        with self._lock:
            lag_row = self._connection.execute(
                """SELECT COALESCE(MAX(latest_sequence - COALESCE(projected_sequence, 0)), 0)
                FROM (
                    SELECT campaign_id, MAX(sequence) AS latest_sequence
                    FROM beta_campaign_events GROUP BY campaign_id
                ) latest
                LEFT JOIN execution_monitor_projections projection
                    ON projection.execution_id = latest.campaign_id"""
            ).fetchone()
            last_row = self._connection.execute("SELECT MAX(created_at_ms) FROM beta_campaign_events").fetchone()
        return {
            "projection_lag": int(lag_row[0] or 0),
            "transaction_failures": self._monitor_transaction_failures,
            "last_event_at_ms": None if last_row[0] is None else int(last_row[0]),
        }

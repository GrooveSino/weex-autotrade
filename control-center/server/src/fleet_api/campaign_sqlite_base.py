from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from threading import RLock
from typing import Any

from weex_cli.beta_campaign import BetaVolumeCampaign

from .campaign_contracts import ACTIVE_STATUSES, CampaignRecord, ExecutionMonitorProjection
from .models import BetaCampaignStatus
from .service import UnsafeOperation


class SQLiteCampaignJournalBase:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS beta_campaigns (
                campaign_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                campaign_json TEXT NOT NULL,
                status TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                result_json TEXT,
                created_at_ms INTEGER NOT NULL,
                updated_at_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_beta_campaigns_instance
                ON beta_campaigns(instance_id, updated_at_ms DESC);
            CREATE TABLE IF NOT EXISTS beta_campaign_events (
                campaign_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                payload TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                PRIMARY KEY(campaign_id, sequence),
                FOREIGN KEY(campaign_id) REFERENCES beta_campaigns(campaign_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS campaign_event_sequences (
                campaign_id TEXT PRIMARY KEY,
                next_sequence INTEGER NOT NULL,
                FOREIGN KEY(campaign_id) REFERENCES beta_campaigns(campaign_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS execution_monitor_projections (
                owner_user_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                execution_id TEXT PRIMARY KEY,
                session_id TEXT,
                executor_generation TEXT NOT NULL,
                projected_sequence INTEGER NOT NULL,
                projection_version INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                updated_at_ms INTEGER NOT NULL,
                FOREIGN KEY(execution_id) REFERENCES beta_campaigns(campaign_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_execution_monitor_owner_account
                ON execution_monitor_projections(owner_user_id, account_id, updated_at_ms DESC);
            """
        )
        self._connection.execute(
            """INSERT OR IGNORE INTO campaign_event_sequences(campaign_id, next_sequence)
            SELECT campaigns.campaign_id, COALESCE(MAX(events.sequence), 0) + 1
            FROM beta_campaigns AS campaigns
            LEFT JOIN beta_campaign_events AS events ON events.campaign_id = campaigns.campaign_id
            GROUP BY campaigns.campaign_id"""
        )
        self._connection.commit()
        self._monitor_transaction_failures = 0
        self._lock = RLock()

    def create(self, instance_id: str, campaign: BetaVolumeCampaign, metadata: dict[str, Any]) -> None:
        with self._lock:
            if self.active_for_instance(instance_id) is not None:
                raise UnsafeOperation("this account already has an active Beta Campaign")
            now_ms = int(time.time() * 1000)
            try:
                with self._connection:
                    self._connection.execute(
                        "INSERT INTO beta_campaigns("
                        "campaign_id, instance_id, campaign_json, status, metadata_json, created_at_ms, updated_at_ms"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            campaign.campaign_id,
                            instance_id,
                            json.dumps(campaign.as_dict(), separators=(",", ":")),
                            BetaCampaignStatus.PLANNED.value,
                            json.dumps(metadata, separators=(",", ":")),
                            now_ms,
                            now_ms,
                        ),
                    )
                    self._connection.execute(
                        "INSERT INTO campaign_event_sequences(campaign_id, next_sequence) VALUES (?, 1)",
                        (campaign.campaign_id,),
                    )
            except sqlite3.IntegrityError as exc:
                raise UnsafeOperation("campaign ID already exists") from exc

    def get(self, campaign_id: str) -> CampaignRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM beta_campaigns WHERE campaign_id = ?", (campaign_id.lower(),)
            ).fetchone()
            if row is None:
                return None
            events = self._connection.execute(
                "SELECT payload FROM beta_campaign_events WHERE campaign_id = ? ORDER BY sequence",
                (campaign_id.lower(),),
            ).fetchall()
        return self._record(row, events)

    def list_for_instance(self, instance_id: str) -> list[CampaignRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM beta_campaigns WHERE instance_id = ? ORDER BY updated_at_ms DESC", (instance_id,)
            ).fetchall()
            event_rows = {
                str(row[0]): self._connection.execute(
                    "SELECT payload FROM beta_campaign_events WHERE campaign_id = ? ORDER BY sequence", (row[0],)
                ).fetchall()
                for row in rows
            }
        return [self._record(row, event_rows[str(row[0])]) for row in rows]

    def list_all(self) -> list[CampaignRecord]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM beta_campaigns ORDER BY updated_at_ms DESC").fetchall()
            event_rows = {
                str(row[0]): self._connection.execute(
                    "SELECT payload FROM beta_campaign_events WHERE campaign_id = ? ORDER BY sequence", (row[0],)
                ).fetchall()
                for row in rows
            }
        return [self._record(row, event_rows[str(row[0])]) for row in rows]

    def active_for_instance(self, instance_id: str) -> CampaignRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM beta_campaigns "
                "WHERE instance_id = ? AND status IN (?, ?, ?, ?) "
                "ORDER BY updated_at_ms DESC LIMIT 1",
                (instance_id, *ACTIVE_STATUSES),
            ).fetchone()
        return self._record(row, []) if row else None

    def monitor_record(self, instance_id: str, session_id: str | None = None) -> CampaignRecord | None:
        query = "SELECT * FROM beta_campaigns WHERE instance_id = ?"
        parameters: list[object] = [instance_id]
        if session_id is not None:
            query += " AND json_extract(metadata_json, '$.session_id') = ?"
            parameters.append(session_id)
        query += (
            " ORDER BY CASE WHEN status IN ('planned', 'executing', 'stopping', 'recovering') THEN 0 ELSE 1 END, "
            "updated_at_ms DESC LIMIT 1"
        )
        with self._lock:
            row = self._connection.execute(query, parameters).fetchone()
        return self._record(row, []) if row else None

    def events_after(self, campaign_id: str, sequence: int, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM beta_campaign_events WHERE campaign_id = ? AND sequence > ? "
                "ORDER BY sequence LIMIT ?",
                (campaign_id.lower(), sequence, limit),
            ).fetchall()
        return [json.loads(str(row[0])) for row in rows]

    def events_before(self, campaign_id: str, sequence: int | None, limit: int) -> list[dict[str, Any]]:
        query = "SELECT payload FROM beta_campaign_events WHERE campaign_id = ?"
        parameters: list[object] = [campaign_id.lower()]
        if sequence is not None:
            query += " AND sequence < ?"
            parameters.append(sequence)
        query += " ORDER BY sequence DESC LIMIT ?"
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [json.loads(str(row[0])) for row in reversed(rows)]

    def update(
        self, campaign_id: str, *, status: str | None = None, result: dict[str, Any] | None = None, **metadata: Any
    ) -> None:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT status, metadata_json, result_json FROM beta_campaigns WHERE campaign_id = ?",
                (campaign_id.lower(),),
            ).fetchone()
            if row is None:
                raise KeyError(campaign_id)
            current_status, current_metadata, current_result = row
            merged = {**json.loads(current_metadata), **metadata}
            self._connection.execute(
                "UPDATE beta_campaigns SET status = ?, metadata_json = ?, result_json = ?, "
                "updated_at_ms = ? WHERE campaign_id = ?",
                (
                    status or current_status,
                    json.dumps(merged, separators=(",", ":")),
                    json.dumps(result, separators=(",", ":")) if result is not None else current_result,
                    int(time.time() * 1000),
                    campaign_id.lower(),
                ),
            )

    def claim_execution(self, campaign_id: str, *, started_at_ms: int) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE beta_campaigns SET status = ?, "
                "metadata_json = json_set(metadata_json, '$.risk_acknowledged', 1, '$.started_at_ms', ?), "
                "updated_at_ms = ? WHERE campaign_id = ? AND status = ?",
                (
                    BetaCampaignStatus.EXECUTING.value,
                    started_at_ms,
                    started_at_ms,
                    campaign_id.lower(),
                    BetaCampaignStatus.PLANNED.value,
                ),
            )
            return cursor.rowcount == 1

    def recover_incomplete(self) -> int:
        with self._lock, self._connection:
            now_ms = int(time.time() * 1000)
            cursor = self._connection.execute(
                "UPDATE beta_campaigns SET status = ?, metadata_json = json_set(metadata_json, '$.reason', ?), "
                "updated_at_ms = ? WHERE status IN (?, ?)",
                (
                    BetaCampaignStatus.RECOVERING.value,
                    "control_plane_restart",
                    now_ms,
                    BetaCampaignStatus.EXECUTING.value,
                    BetaCampaignStatus.STOPPING.value,
                ),
            )
            return int(cursor.rowcount)

    def remove(self, instance_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM beta_campaigns WHERE instance_id = ?", (instance_id,))

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _record(row: tuple[object, ...], events: list[tuple[object, ...]]) -> CampaignRecord:
        return CampaignRecord(
            campaign_id=str(row[0]),
            instance_id=str(row[1]),
            campaign=BetaVolumeCampaign.from_dict(json.loads(str(row[2]))),
            status=str(row[3]),
            metadata=json.loads(str(row[4])),
            result=json.loads(str(row[5])) if row[5] else None,
            events=tuple(json.loads(str(event[0])) for event in events),
        )

    @staticmethod
    def _projection(row: tuple[object, ...]) -> ExecutionMonitorProjection:
        return ExecutionMonitorProjection(
            owner_user_id=str(row[0]),
            account_id=str(row[1]),
            execution_id=str(row[2]),
            session_id=None if row[3] is None else str(row[3]),
            executor_generation=str(row[4]),
            projected_sequence=int(row[5]),
            projection_version=int(row[6]),
            state=json.loads(str(row[7])),
            updated_at_ms=int(row[8]),
        )

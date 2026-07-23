from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from weex_cli.beta_allocation import BetaUnavailable, HttpBetaAllocationProvider
from weex_cli.beta_campaign import (
    BetaVolumeCampaign,
    BetaVolumeCampaignStore,
    LiveBetaVolumeCampaignService,
    campaign_confirmation,
    inspect_live_account,
    live_profile_fingerprint,
)
from weex_cli.beta_volume import BetaVolumePlanStore
from weex_cli.config import Credentials, Settings
from weex_cli.execution_progress import (
    EXECUTION_PROGRESS_PROJECTION_VERSION,
    ExecutionProgressProjector,
)
from weex_cli.errors import SafetyError
from weex_cli.gateway import WeexGateway
from weex_cli.live_profile import LiveProfile
from weex_cli.live_websocket import WeexCampaignWebSocketRuntime

from .config import ControlPlaneSettings
from .models import (
    BetaCampaignEvent,
    BetaCampaignPreview,
    BetaCampaignPreviewRequest,
    BetaCampaignStatus,
    BetaCampaignView,
    VolumeStrategy,
)
from .ownership import LEGACY_OWNER_USER_ID
from .service import BetaSourceUnavailable, UnsafeOperation, ValidationFailed
from .vault import CredentialMaterial, CredentialVault


class CampaignJournal(Protocol):
    def create(self, instance_id: str, campaign: BetaVolumeCampaign, metadata: dict[str, Any]) -> None: ...

    def get(self, campaign_id: str) -> CampaignRecord | None: ...

    def list_for_instance(self, instance_id: str) -> list[CampaignRecord]: ...

    def list_all(self) -> list[CampaignRecord]: ...

    def active_for_instance(self, instance_id: str) -> CampaignRecord | None: ...

    def monitor_record(self, instance_id: str, session_id: str | None = None) -> CampaignRecord | None: ...

    def events_after(self, campaign_id: str, sequence: int, limit: int) -> list[dict[str, Any]]: ...

    def events_before(self, campaign_id: str, sequence: int | None, limit: int) -> list[dict[str, Any]]: ...

    def update(
        self, campaign_id: str, *, status: str | None = None, result: dict[str, Any] | None = None, **metadata: Any
    ) -> None: ...

    def claim_execution(self, campaign_id: str, *, started_at_ms: int) -> bool: ...

    def add_event(self, campaign_id: str, event: dict[str, Any]) -> int: ...

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
    ) -> int: ...

    def monitor_projection(self, campaign_id: str) -> ExecutionMonitorProjection | None: ...

    def replace_monitor_projection(self, projection: ExecutionMonitorProjection) -> None: ...

    def monitor_read(
        self, campaign_id: str, before_sequence: int | None, limit: int
    ) -> tuple[ExecutionMonitorProjection | None, list[dict[str, Any]], int]: ...

    def monitor_metrics(self) -> dict[str, int | None]: ...

    def recover_incomplete(self) -> int: ...

    def remove(self, instance_id: str) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class CampaignRecord:
    campaign_id: str
    instance_id: str
    campaign: BetaVolumeCampaign
    status: str
    metadata: dict[str, Any]
    result: dict[str, Any] | None
    events: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ExecutionMonitorProjection:
    owner_user_id: str
    account_id: str
    execution_id: str
    session_id: str | None
    executor_generation: str
    projected_sequence: int
    projection_version: int
    state: dict[str, Any]
    updated_at_ms: int


class _AccountLease:
    def __init__(self, root: Path, api_key: str, instance_id: str, campaign_id: str) -> None:
        fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]
        self.path = root / "locks" / f"account-{fingerprint}.lock"
        self.instance_id = instance_id
        self.campaign_id = campaign_id
        self._handle: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise UnsafeOperation("this WEEX account is already in use by another live campaign") from None
        handle.seek(0)
        handle.truncate()
        json.dump(
            {"pid": os.getpid(), "instance_id": self.instance_id, "campaign_id": self.campaign_id},
            handle,
            separators=(",", ":"),
        )
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


ACTIVE_STATUSES = {
    BetaCampaignStatus.PLANNED.value,
    BetaCampaignStatus.EXECUTING.value,
    BetaCampaignStatus.STOPPING.value,
}


class InMemoryCampaignJournal:
    def __init__(self) -> None:
        self._records: dict[str, CampaignRecord] = {}
        self._monitor_projections: dict[str, ExecutionMonitorProjection] = {}
        self._monitor_transaction_failures = 0
        self._lock = RLock()

    def create(self, instance_id: str, campaign: BetaVolumeCampaign, metadata: dict[str, Any]) -> None:
        with self._lock:
            if self.active_for_instance(instance_id) is not None:
                raise UnsafeOperation("this account already has an active Beta Campaign")
            if campaign.campaign_id in self._records:
                raise UnsafeOperation("campaign ID already exists")
            self._records[campaign.campaign_id] = CampaignRecord(
                campaign.campaign_id, instance_id, campaign, BetaCampaignStatus.PLANNED.value, dict(metadata), None, ()
            )

    def get(self, campaign_id: str) -> CampaignRecord | None:
        with self._lock:
            return self._records.get(campaign_id.lower())

    def list_for_instance(self, instance_id: str) -> list[CampaignRecord]:
        with self._lock:
            return [record for record in self._records.values() if record.instance_id == instance_id]

    def list_all(self) -> list[CampaignRecord]:
        with self._lock:
            return list(self._records.values())

    def active_for_instance(self, instance_id: str) -> CampaignRecord | None:
        return next(
            (record for record in self.list_for_instance(instance_id) if record.status in ACTIVE_STATUSES), None
        )

    def monitor_record(self, instance_id: str, session_id: str | None = None) -> CampaignRecord | None:
        records = self.list_for_instance(instance_id)
        if session_id is not None:
            records = [record for record in records if record.metadata.get("session_id") == session_id]
        return next(
            (record for record in records if record.status in ACTIVE_STATUSES), records[-1] if records else None
        )

    def events_after(self, campaign_id: str, sequence: int, limit: int) -> list[dict[str, Any]]:
        record = self.get(campaign_id)
        if record is None:
            return []
        return [dict(event) for event in record.events if int(event.get("sequence") or 0) > sequence][:limit]

    def events_before(self, campaign_id: str, sequence: int | None, limit: int) -> list[dict[str, Any]]:
        record = self.get(campaign_id)
        if record is None:
            return []
        selected = [
            dict(event) for event in record.events if sequence is None or int(event.get("sequence") or 0) < sequence
        ]
        return selected[-limit:]

    def update(
        self, campaign_id: str, *, status: str | None = None, result: dict[str, Any] | None = None, **metadata: Any
    ) -> None:
        with self._lock:
            current = self._records[campaign_id.lower()]
            merged = {**current.metadata, **metadata}
            self._records[campaign_id.lower()] = CampaignRecord(
                current.campaign_id,
                current.instance_id,
                current.campaign,
                status or current.status,
                merged,
                result if result is not None else current.result,
                current.events,
            )

    def add_event(self, campaign_id: str, event: dict[str, Any]) -> int:
        with self._lock:
            current = self._records[campaign_id.lower()]
            sequence = len(current.events) + 1
            stored_event = {**event, "sequence": sequence}
            self._records[campaign_id.lower()] = CampaignRecord(
                current.campaign_id,
                current.instance_id,
                current.campaign,
                current.status,
                current.metadata,
                current.result,
                (*current.events, stored_event),
            )
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
        with self._lock:
            try:
                current = self._records[campaign_id.lower()]
                sequence = len(current.events) + 1
                stored_event = json.loads(json.dumps({**event, "sequence": sequence}, separators=(",", ":")))
                projected_state = state
                if projected_state is None:
                    existing_projection = self._monitor_projections.get(current.campaign_id)
                    projector = ExecutionProgressProjector.from_snapshot(
                        existing_projection.state if existing_projection is not None else None
                    )
                    projector.apply(stored_event, at_ms=int(stored_event.get("at_ms") or 0))
                    projected_state = projector.snapshot()
                stored_state = json.loads(json.dumps(projected_state, separators=(",", ":")))
                now_ms = int(event.get("at_ms") or time.time() * 1000)
                projection = ExecutionMonitorProjection(
                    owner_user_id=owner_user_id,
                    account_id=account_id,
                    execution_id=current.campaign_id,
                    session_id=session_id,
                    executor_generation=executor_generation,
                    projected_sequence=sequence,
                    projection_version=projection_version,
                    state=stored_state,
                    updated_at_ms=now_ms,
                )
                existing = self._monitor_projections.get(current.campaign_id)
                if existing is not None and existing.owner_user_id != owner_user_id:
                    raise UnsafeOperation("execution monitor owner mismatch")
                metadata = {
                    **current.metadata,
                    "monitor_state": stored_state,
                    "phase": stored_state.get("phase", current.metadata.get("phase")),
                    "current_run": stored_state.get("current_run", current.metadata.get("current_run")),
                }
                self._records[current.campaign_id] = CampaignRecord(
                    current.campaign_id,
                    current.instance_id,
                    current.campaign,
                    current.status,
                    metadata,
                    current.result,
                    (*current.events, stored_event),
                )
                self._monitor_projections[current.campaign_id] = projection
                return sequence
            except Exception:
                self._monitor_transaction_failures += 1
                raise

    def monitor_projection(self, campaign_id: str) -> ExecutionMonitorProjection | None:
        with self._lock:
            return self._monitor_projections.get(campaign_id.lower())

    def replace_monitor_projection(self, projection: ExecutionMonitorProjection) -> None:
        with self._lock:
            current = self._monitor_projections.get(projection.execution_id.lower())
            if current is not None and current.owner_user_id != projection.owner_user_id:
                raise UnsafeOperation("execution monitor owner mismatch")
            if current is not None and current.projected_sequence > projection.projected_sequence:
                return
            self._monitor_projections[projection.execution_id.lower()] = projection

    def monitor_read(
        self, campaign_id: str, before_sequence: int | None, limit: int
    ) -> tuple[ExecutionMonitorProjection | None, list[dict[str, Any]], int]:
        with self._lock:
            record = self._records.get(campaign_id.lower())
            if record is None:
                return None, [], 0
            rows = [
                dict(event)
                for event in record.events
                if before_sequence is None or int(event.get("sequence") or 0) < before_sequence
            ][-limit:]
            return self._monitor_projections.get(campaign_id.lower()), rows, len(record.events)

    def monitor_metrics(self) -> dict[str, int | None]:
        with self._lock:
            lag = max(
                (
                    len(record.events)
                    - (
                        self._monitor_projections[record.campaign_id].projected_sequence
                        if record.campaign_id in self._monitor_projections
                        else 0
                    )
                    for record in self._records.values()
                    if record.events
                ),
                default=0,
            )
            latest = max(
                (int(event.get("at_ms") or 0) for record in self._records.values() for event in record.events),
                default=None,
            )
            return {
                "projection_lag": lag,
                "transaction_failures": self._monitor_transaction_failures,
                "last_event_at_ms": latest,
            }

    def claim_execution(self, campaign_id: str, *, started_at_ms: int) -> bool:
        with self._lock:
            current = self._records[campaign_id.lower()]
            if current.status != BetaCampaignStatus.PLANNED.value:
                return False
            self.update(
                campaign_id,
                status=BetaCampaignStatus.EXECUTING.value,
                risk_acknowledged=True,
                started_at_ms=started_at_ms,
            )
            return True

    def recover_incomplete(self) -> int:
        count = 0
        with self._lock:
            for record in tuple(self._records.values()):
                if record.status in {BetaCampaignStatus.EXECUTING.value, BetaCampaignStatus.STOPPING.value}:
                    self.update(
                        record.campaign_id, status=BetaCampaignStatus.UNCERTAIN.value, reason="control_plane_restart"
                    )
                    count += 1
        return count

    def remove(self, instance_id: str) -> None:
        with self._lock:
            for campaign_id in [
                record.campaign_id for record in self._records.values() if record.instance_id == instance_id
            ]:
                self._records.pop(campaign_id, None)
                self._monitor_projections.pop(campaign_id, None)

    def close(self) -> None:
        return None


class SQLiteCampaignJournal:
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
                "WHERE instance_id = ? AND status IN (?, ?, ?) "
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
            " ORDER BY CASE WHEN status IN ('planned', 'executing', 'stopping') THEN 0 ELSE 1 END, "
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

    def add_event(self, campaign_id: str, event: dict[str, Any]) -> int:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM beta_campaign_events WHERE campaign_id = ?",
                    (campaign_id.lower(),),
                ).fetchone()
                sequence = int(row[0]) + 1
                stored_event = {**event, "sequence": sequence}
                self._connection.execute(
                    "INSERT INTO beta_campaign_events(campaign_id, sequence, payload, created_at_ms) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        campaign_id.lower(),
                        sequence,
                        json.dumps(stored_event, separators=(",", ":")),
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
                sequence_row = self._connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM beta_campaign_events WHERE campaign_id = ?",
                    (normalized_campaign_id,),
                ).fetchone()
                sequence = int(sequence_row[0]) + 1
                stored_event = {**event, "sequence": sequence}
                event_json = json.dumps(stored_event, separators=(",", ":"))
                projected_state = state
                if projected_state is None:
                    projector = ExecutionProgressProjector.from_snapshot(
                        json.loads(str(existing[1])) if existing is not None else None
                    )
                    projector.apply(stored_event, at_ms=int(stored_event.get("at_ms") or 0))
                    projected_state = projector.snapshot()
                state_json = json.dumps(projected_state, separators=(",", ":"))
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
                    (json.dumps(metadata, separators=(",", ":")), now_ms, normalized_campaign_id),
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
                    json.dumps(projection.state, separators=(",", ":")),
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
                    "SELECT COALESCE(MAX(sequence), 0) FROM beta_campaign_events WHERE campaign_id = ?",
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
            last_row = self._connection.execute(
                "SELECT MAX(created_at_ms) FROM beta_campaign_events"
            ).fetchone()
        return {
            "projection_lag": int(lag_row[0] or 0),
            "transaction_failures": self._monitor_transaction_failures,
            "last_event_at_ms": None if last_row[0] is None else int(last_row[0]),
        }

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
                    BetaCampaignStatus.UNCERTAIN.value,
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


class CampaignWorkerManager:
    def __init__(
        self,
        settings: ControlPlaneSettings,
        vault: CredentialVault,
        journal: CampaignJournal,
        beta_provider_factory: Callable[[], HttpBetaAllocationProvider],
        *,
        on_change: Callable[[str], None] | None = None,
        on_progress: Callable[[str, Mapping[str, Any]], None] | None = None,
        on_execution_claim: Callable[[CampaignRecord, int], None] | None = None,
        executor_generation: str = "local",
    ) -> None:
        self.settings = settings
        self.vault = vault
        self.journal = journal
        self.beta_provider_factory = beta_provider_factory
        self.on_change = on_change or (lambda _instance_id: None)
        self.on_progress = on_progress or (lambda _instance_id, _event: None)
        self.on_execution_claim = on_execution_claim or (lambda _record, _started_at_ms: None)
        self.executor_generation = executor_generation
        self._executor = ThreadPoolExecutor(
            max_workers=settings.live_campaign_worker_count, thread_name_prefix="weex-campaign"
        )
        self._stops: dict[str, threading.Event] = {}
        self._futures: dict[str, Future[None]] = {}
        self._leases: dict[str, _AccountLease] = {}
        self._starting: set[str] = set()
        self._closing = False
        self._lock = RLock()

    def recover(self) -> int:
        count = self.journal.recover_incomplete()
        for record in self.journal.list_all():
            self._notify(record.instance_id)
        return count

    def preview(
        self,
        instance_id: str,
        request: BetaCampaignPreviewRequest,
        material: CredentialMaterial | None,
        *,
        owner_user_id: str = LEGACY_OWNER_USER_ID,
    ) -> BetaCampaignPreview:
        self._require_live_gate()
        if material is None:
            raise UnsafeOperation("account credentials are unavailable")
        if self.journal.active_for_instance(instance_id) is not None:
            raise UnsafeOperation("this account already has an active Beta Campaign")
        if self._unresolved_uncertain(instance_id) is not None:
            raise UnsafeOperation("this account has an uncertain Beta Campaign that requires manual reconciliation")
        profile, gateway = self._profile_and_gateway(material)
        provider = self.beta_provider_factory()
        try:
            try:
                allocation = provider.get()
            except BetaUnavailable as exc:
                raise BetaSourceUnavailable(f"final beta source unavailable: {exc}") from None
            campaign = BetaVolumeCampaign.create(
                gateway,
                allocation,
                profile_fingerprint=live_profile_fingerprint(profile),
                target_turnover_quote=request.target_quote,
                round_turnover_quote=request.cycle_volume,
                hold_min_seconds=request.hold_min_seconds,
                hold_max_seconds=request.hold_max_seconds,
                round_gap_min_seconds=request.round_gap_min_seconds,
                round_gap_max_seconds=request.round_gap_max_seconds,
            )
            opening_notional = min(campaign.round_turnover_quote, campaign.target_turnover_quote) / Decimal(2)
            required = opening_notional / Decimal(campaign.max_auto_leverage) * campaign.margin_buffer
            readiness = inspect_live_account(
                gateway,
                required,
                opening_notional=opening_notional,
                leverage=campaign.leverage,
                max_auto_leverage=campaign.max_auto_leverage,
                margin_buffer=campaign.margin_buffer,
            )
            available = _available_quote_from_readiness(readiness)
            blockers: list[str] = []
            if not readiness.get("available_sufficient", False):
                blockers.append("available_balance_insufficient")
            if (
                readiness.get("active_position_count", 0)
                or readiness.get("regular_order_count", 0)
                or readiness.get("trigger_order_count", 0)
            ):
                blockers.append("account_is_not_flat")
            if blockers:
                raise UnsafeOperation(f"campaign preview blocked: {','.join(blockers)}")
            metadata = _preview_metadata(campaign, available, readiness)
            metadata["owner_user_id"] = owner_user_id
            self.journal.create(instance_id, campaign, metadata)
            BetaVolumeCampaignStore(self.settings.campaign_data_directory / instance_id).create(campaign)
            return _view(self.journal.get(campaign.campaign_id), include_events=False)  # type: ignore[arg-type]
        finally:
            gateway.close()

    def preview_bound_strategy(
        self,
        instance_id: str,
        strategy: VolumeStrategy,
        target_quote: Decimal,
        material: CredentialMaterial | None,
        *,
        session_id: str | None,
        target_mode: str = "incremental",
        run_disposition: str = "new_incremental",
        strategy_target_quote: Decimal | None = None,
        baseline_lifetime_quote: Decimal = Decimal(0),
        owner_user_id: str = LEGACY_OWNER_USER_ID,
    ) -> BetaCampaignPreview:
        """Create an executable Live preview solely from a persisted strategy binding."""
        self._require_live_gate()
        if target_quote <= 0:
            raise UnsafeOperation("bound strategy has no remaining verified target")
        if material is None:
            raise UnsafeOperation("account credentials are unavailable")
        invalidated = self._invalidate_stale_preview_for_current_strategy(instance_id, strategy)
        if self.journal.active_for_instance(instance_id) is not None:
            raise UnsafeOperation("this account already has an active bound strategy execution")
        if self._unresolved_uncertain(instance_id) is not None:
            raise UnsafeOperation("this account has an uncertain execution that requires manual reconciliation")
        if invalidated:
            self._notify(instance_id)
        profile, gateway = self._profile_and_gateway(material)
        provider = self.beta_provider_factory()
        try:
            try:
                allocation = provider.get()
            except BetaUnavailable as exc:
                raise BetaSourceUnavailable(f"final beta source unavailable: {exc}") from None
            campaign = BetaVolumeCampaign.create(
                gateway,
                allocation,
                profile_fingerprint=live_profile_fingerprint(profile),
                target_turnover_quote=target_quote,
                round_turnover_quote=strategy.round_turnover_quote_max,
                round_turnover_quote_min=strategy.round_turnover_quote_min,
                hold_min_seconds=strategy.position_hold_min_seconds,
                hold_max_seconds=strategy.position_hold_max_seconds,
                round_gap_min_seconds=strategy.round_interval_min_seconds,
                round_gap_max_seconds=strategy.round_interval_max_seconds,
            )
            opening_notional = min(campaign.round_turnover_quote, campaign.target_turnover_quote) / Decimal(2)
            required = opening_notional / Decimal(campaign.max_auto_leverage) * campaign.margin_buffer
            readiness = inspect_live_account(
                gateway,
                required,
                opening_notional=opening_notional,
                leverage=campaign.leverage,
                max_auto_leverage=campaign.max_auto_leverage,
                margin_buffer=campaign.margin_buffer,
            )
            available = _available_quote_from_readiness(readiness)
            blockers: list[str] = []
            if not readiness.get("available_sufficient", False):
                blockers.append("available_balance_insufficient")
            if (
                readiness.get("active_position_count", 0)
                or readiness.get("regular_order_count", 0)
                or readiness.get("trigger_order_count", 0)
            ):
                blockers.append("account_is_not_flat")
            if blockers:
                raise UnsafeOperation(f"bound strategy preview blocked: {','.join(blockers)}")
            metadata = _preview_metadata(campaign, available, readiness)
            metadata.update(
                {
                    "execution_kind": "bound_strategy",
                    "confirmation": _bound_strategy_confirmation(campaign),
                    "stop_confirmation": _bound_strategy_stop_confirmation(campaign.campaign_id),
                    "strategy_id": strategy.id,
                    "strategy_name": strategy.name,
                    "strategy_version": strategy.version,
                    "strategy_snapshot": strategy.model_dump(mode="json", by_alias=True),
                    "session_id": session_id,
                    "session_target_quote": str(target_quote),
                    "target_mode": target_mode,
                    "run_disposition": run_disposition,
                    "strategy_target_quote": str(strategy_target_quote or target_quote),
                    "baseline_lifetime_quote": str(baseline_lifetime_quote),
                    "owner_user_id": owner_user_id,
                }
            )
            self.journal.create(instance_id, campaign, metadata)
            BetaVolumeCampaignStore(self.settings.campaign_data_directory / instance_id).create(campaign)
            return _view(self.journal.get(campaign.campaign_id), include_events=False)  # type: ignore[arg-type]
        finally:
            gateway.close()

    def apply_bound_strategy_change(
        self,
        instance_ids: Iterable[str],
        apply: Callable[[], Any],
        *,
        reason: str,
    ) -> Any:
        """Atomically persist a binding change and retire only its unexecuted previews.

        Planned bound-strategy previews are immutable authorization artifacts.  They
        cannot be edited to match a new shared strategy, because that would let an
        old exact confirmation authorize a different execution.  They are safe to
        retire: no worker has claimed them and no exchange operation has occurred.
        """
        affected = tuple(dict.fromkeys(instance_ids))
        with self._lock:
            self._assert_bound_strategy_change_allowed(affected)
            result = apply()
            invalidated = self._invalidate_planned_bound_strategy_previews_locked(affected, reason=reason)
        for instance_id in invalidated:
            self._notify(instance_id)
        return result

    def invalidate_stale_planned_bound_strategy_previews(
        self,
        strategies_by_instance: Mapping[str, VolumeStrategy],
        *,
        reason: str,
    ) -> list[str]:
        """Retire persisted previews whose immutable strategy snapshot is stale.

        This is used during executor startup to repair planned previews created by
        older releases, before they can shadow an account's current binding.
        """
        with self._lock:
            invalidated: list[str] = []
            for instance_id, strategy in strategies_by_instance.items():
                record = self.journal.active_for_instance(instance_id)
                if record is None or not self._is_stale_bound_strategy_preview(record, strategy):
                    continue
                self._invalidate_planned_record_locked(record, reason=reason)
                invalidated.append(instance_id)
        for instance_id in invalidated:
            self._notify(instance_id)
        return invalidated

    def start(
        self,
        instance_id: str,
        campaign_id: str,
        confirmation: str,
        risk_acknowledged: bool,
        material: CredentialMaterial | None,
    ) -> BetaCampaignView:
        self._require_live_gate()
        if not risk_acknowledged:
            raise UnsafeOperation("risk acknowledgement is required")
        record = self._require_record(instance_id, campaign_id)
        if record.status != BetaCampaignStatus.PLANNED.value:
            raise UnsafeOperation("campaign is not in planned state")
        if record.campaign.schema_version not in {2, 3}:
            raise UnsafeOperation("execution schema is not executable; create a new preview")
        with self._lock:
            if self._closing:
                raise UnsafeOperation("campaign manager is shutting down")
            if campaign_id in self._starting or campaign_id in self._futures:
                raise UnsafeOperation("campaign is already starting or running")
            self._starting.add(campaign_id)
        lease: _AccountLease | None = None
        submitted = False
        try:
            if int(time.time() * 1000) >= record.campaign.expires_at_ms:
                raise UnsafeOperation("campaign authorization has expired")
            if confirmation != str(record.metadata["confirmation"]):
                raise UnsafeOperation("exact campaign confirmation does not match")
            if material is None:
                raise UnsafeOperation("account credentials are unavailable")
            lease = _AccountLease(
                self.settings.campaign_data_directory,
                material.api_key.get_secret_value(),
                instance_id,
                campaign_id,
            )
            lease.acquire()
            starting_available_balance = self._verify_execution_boundary(record, material)
            self.journal.update(
                campaign_id,
                starting_available_balance_quote=str(starting_available_balance),
            )
            stop = threading.Event()
            with self._lock:
                started_at_ms = int(time.time() * 1000)
                if not self.journal.claim_execution(campaign_id, started_at_ms=started_at_ms):
                    raise UnsafeOperation("campaign was already claimed by another worker")
                claimed = self._require_record(instance_id, campaign_id)
                try:
                    self.on_execution_claim(claimed, started_at_ms)
                except Exception as exc:
                    self.journal.update(
                        campaign_id,
                        status=BetaCampaignStatus.UNCERTAIN.value,
                        reason=f"execution_claim_callback_failed:{type(exc).__name__.lower()}",
                    )
                    raise UnsafeOperation("execution could not establish its local ledger session") from exc
                self._stops[campaign_id] = stop
                self._leases[campaign_id] = lease
                claimed = self._require_record(instance_id, campaign_id)
                try:
                    future = self._executor.submit(self._run, claimed, material, stop)
                except Exception as exc:
                    self._stops.pop(campaign_id, None)
                    self._leases.pop(campaign_id, None)
                    self.journal.update(
                        campaign_id,
                        status=BetaCampaignStatus.UNCERTAIN.value,
                        reason=f"worker_submit_failed:{type(exc).__name__.lower()}",
                    )
                    raise UnsafeOperation(
                        "campaign worker could not be started; manual reconciliation is required"
                    ) from exc
                self._futures[campaign_id] = future
                submitted = True
        finally:
            with self._lock:
                self._starting.discard(campaign_id)
            if lease is not None and not submitted:
                lease.release()
        self._notify(instance_id)
        return _view(self.journal.get(campaign_id), include_events=False)  # type: ignore[arg-type]

    def stop(self, instance_id: str, campaign_id: str, confirmation: str) -> BetaCampaignView:
        record = self._require_record(instance_id, campaign_id)
        if record.status not in {BetaCampaignStatus.EXECUTING.value, BetaCampaignStatus.STOPPING.value}:
            raise UnsafeOperation("campaign is not running")
        if confirmation != str(record.metadata["stop_confirmation"]):
            raise UnsafeOperation("exact stop confirmation does not match")
        with self._lock:
            event = self._stops.get(campaign_id)
            if event is None:
                raise UnsafeOperation("campaign worker is not available")
            event.set()
            self.journal.update(campaign_id, status=BetaCampaignStatus.STOPPING.value, reason="stop_requested")
        self._notify(instance_id)
        return _view(self.journal.get(campaign_id), include_events=False)  # type: ignore[arg-type]

    def get(self, instance_id: str, campaign_id: str) -> BetaCampaignView:
        return _view(self._require_record(instance_id, campaign_id))

    def list(self, instance_id: str) -> list[BetaCampaignView]:
        return [_view(record, include_events=False) for record in self.journal.list_for_instance(instance_id)]

    def events(self, instance_id: str, campaign_id: str) -> list[BetaCampaignEvent]:
        record = self._require_record(instance_id, campaign_id)
        return [BetaCampaignEvent.model_validate(event) for event in record.events]

    def reconcile(
        self,
        instance_id: str,
        campaign_id: str,
        confirmation: str,
        material: CredentialMaterial | None,
    ) -> BetaCampaignView:
        self._require_live_gate()
        record = self._require_record(instance_id, campaign_id)
        if record.status != BetaCampaignStatus.UNCERTAIN.value:
            raise UnsafeOperation("manual reconciliation is only available for an uncertain campaign")
        if not _reconciliation_required(record):
            return _view(record)
        expected = _reconciliation_confirmation(record.campaign_id)
        if confirmation != expected:
            raise UnsafeOperation("exact reconciliation confirmation does not match")
        if material is None:
            raise UnsafeOperation("account credentials are unavailable")

        profile: LiveProfile | None = None
        gateway: WeexGateway | None = None
        try:
            profile, gateway = self._profile_and_gateway(material)
            if live_profile_fingerprint(profile) != record.campaign.profile_fingerprint:
                raise UnsafeOperation("live profile changed since campaign execution")
            boundary = inspect_live_account(gateway, Decimal(0))
            if not _account_boundary_is_flat(boundary):
                raise UnsafeOperation("manual reconciliation requires flat BTC/ETH positions and no active orders")
        finally:
            if gateway is not None:
                gateway.close()

        reconciled_at_ms = int(time.time() * 1000)
        self.journal.update(
            campaign_id,
            reconciliation_acknowledged_at_ms=reconciled_at_ms,
            reconciliation_boundary="btc_eth_flat_no_regular_or_trigger_orders",
        )
        event = _sanitize_event({"event": "campaign_reconciliation_acknowledged"})
        event["sequence"] = self._append_monitor_event(record, event)
        self._notify(instance_id)
        return _view(self.journal.get(campaign_id))  # type: ignore[arg-type]

    def public_snapshot(self) -> list[dict[str, Any]]:
        if not hasattr(self.journal, "list_all"):
            return []
        return [
            _view(record, include_events=False).model_dump(mode="json", by_alias=True)
            for record in self.journal.list_all()
        ]  # type: ignore[attr-defined]

    def active_worker_count(self) -> int:
        """Return work currently owned by this process, not executor capacity."""
        with self._lock:
            active_futures = {campaign_id for campaign_id, future in self._futures.items() if not future.done()}
            return len(self._starting | active_futures)

    def close(self) -> None:
        with self._lock:
            self._closing = True
            for stop in self._stops.values():
                stop.set()
        self._executor.shutdown(wait=True, cancel_futures=False)
        self.journal.close()

    def _run(self, record: CampaignRecord, material: CredentialMaterial, stop: threading.Event) -> None:
        campaign_id = record.campaign_id
        profile: LiveProfile | None = None
        gateway: WeexGateway | None = None
        snapshot_gateway: WeexGateway | None = None
        lanes: dict[str, WeexGateway] = {}
        websocket_runtime: WeexCampaignWebSocketRuntime | None = None

        def event_sink(payload: dict[str, Any]) -> None:
            event = _sanitize_event(payload)
            sequence = self._append_monitor_event(record, event)
            event["sequence"] = sequence
            self._notify_progress(record.instance_id, event)
            if _publishes_fleet_snapshot(str(event["name"])):
                self._notify(record.instance_id)

        try:
            profile, gateway = self._profile_and_gateway(material)
            provider = self.beta_provider_factory()
            snapshot_gateway = gateway.fork()
            lanes = {"BTC": gateway.fork(), "ETH": gateway.fork()}
            websocket_runtime = WeexCampaignWebSocketRuntime(
                snapshot_gateway,
                profile.settings.require_credentials(),
                proxy_url=profile.proxy_url,
            )
            websocket_runtime.start()
            result = LiveBetaVolumeCampaignService(
                gateway,
                provider,
                BetaVolumeCampaignStore(self.settings.campaign_data_directory / record.instance_id),
                BetaVolumePlanStore(self.settings.campaign_data_directory / record.instance_id / "plans"),
                profile_fingerprint=live_profile_fingerprint(profile),
                event_sink=event_sink,
                lane_gateways=lanes,
                market_data=websocket_runtime,
                order_updates=websocket_runtime,
                stop_requested=stop.is_set,
            ).execute(record.campaign)
            try:
                ending_available_balance = _available_quote(gateway)
            except Exception:  # A missing audit snapshot must not change the execution outcome.
                ending_available_balance = None
            status = str(result.get("status") or BetaCampaignStatus.UNCERTAIN.value)
            if status not in {item.value for item in BetaCampaignStatus}:
                status = BetaCampaignStatus.UNCERTAIN.value
            metrics = _campaign_result_metrics(result)
            self.journal.update(
                campaign_id,
                status=status,
                result=result,
                finished_at_ms=int(time.time() * 1000),
                phase="finished",
                generated_quote=result.get("executed_quote_volume", "0"),
                remaining_quote=result.get("remaining_quote", "0"),
                excess_quote=result.get("excess_quote", "0"),
                reason=result.get("reason"),
                ending_available_balance_quote=(
                    None if ending_available_balance is None else str(ending_available_balance)
                ),
                **metrics,
            )
        except Exception as exc:  # noqa: BLE001 - a worker failure is an uncertain live outcome
            reason = _worker_exception_reason(exc)
            self.journal.update(
                campaign_id,
                status=BetaCampaignStatus.UNCERTAIN.value,
                finished_at_ms=int(time.time() * 1000),
                reason=reason,
            )
            event = _sanitize_event(
                {
                    "event": "campaign_uncertain",
                    "error": type(exc).__name__,
                    "reason": reason,
                }
            )
            event["sequence"] = self._append_monitor_event(record, event)
            self._notify_progress(record.instance_id, event)
        finally:
            if websocket_runtime is not None:
                websocket_runtime.close()
            if snapshot_gateway is not None:
                snapshot_gateway.close()
            for lane in lanes.values():
                lane.close()
            if gateway is not None:
                gateway.close()
            with self._lock:
                self._stops.pop(campaign_id, None)
                self._futures.pop(campaign_id, None)
                lease = self._leases.pop(campaign_id, None)
            if lease is not None:
                lease.release()
            self._notify(record.instance_id)

    def _profile_and_gateway(self, material: CredentialMaterial) -> tuple[LiveProfile, WeexGateway]:
        settings = Settings(
            credentials=Credentials(
                api_key=material.api_key.get_secret_value(),
                api_secret=material.api_secret.get_secret_value(),
                passphrase=material.passphrase.get_secret_value(),
            ),
            default_mode="live",
            live_trading_enabled=True,
            timeout_ms=self.settings.weex_request_timeout_ms,
            enable_rate_limit=True,
        )
        profile = LiveProfile(
            path=self.settings.campaign_data_directory / "control-plane-live.toml",
            settings=settings,
            proxy_url=_normalize_proxy_url(
                material.proxy_url.get_secret_value() if material.proxy_url is not None else None
            ),
            allow_live_mutations=True,
            post_only_only=True,
        )
        profile.require_maker_execution()
        return profile, WeexGateway(settings, proxy_url=profile.proxy_url)

    def _notify_progress(self, instance_id: str, event: Mapping[str, Any]) -> None:
        """Keep observability failures out of the live execution state machine."""
        try:
            self.on_progress(instance_id, event)
        except Exception:
            return

    def _append_monitor_event(
        self,
        record: CampaignRecord,
        event: dict[str, Any],
    ) -> int:
        return self.journal.append_and_project(
            record.campaign_id,
            event,
            owner_user_id=str(record.metadata.get("owner_user_id") or LEGACY_OWNER_USER_ID),
            account_id=record.instance_id,
            session_id=str(record.metadata["session_id"]) if record.metadata.get("session_id") else None,
            executor_generation=self.executor_generation,
            projection_version=EXECUTION_PROGRESS_PROJECTION_VERSION,
        )

    def _invalidate_stale_preview_for_current_strategy(self, instance_id: str, strategy: VolumeStrategy) -> bool:
        with self._lock:
            record = self.journal.active_for_instance(instance_id)
            if record is None or not self._is_stale_bound_strategy_preview(record, strategy):
                return False
            self._invalidate_planned_record_locked(record, reason="bound_strategy_version_stale")
            return True

    def _assert_bound_strategy_change_allowed(self, instance_ids: Iterable[str]) -> None:
        for instance_id in instance_ids:
            for record in self.journal.list_for_instance(instance_id):
                if record.metadata.get("execution_kind") != "bound_strategy":
                    continue
                if record.status in {BetaCampaignStatus.EXECUTING.value, BetaCampaignStatus.STOPPING.value}:
                    raise UnsafeOperation(
                        "cannot change a bound strategy while its Live execution is active; stop and verify it first"
                    )
                if record.status == BetaCampaignStatus.UNCERTAIN.value and _reconciliation_required(record):
                    raise UnsafeOperation(
                        "cannot change a bound strategy while its Live execution requires manual reconciliation"
                    )

    def _invalidate_planned_bound_strategy_previews_locked(
        self, instance_ids: Iterable[str], *, reason: str
    ) -> list[str]:
        invalidated: list[str] = []
        for instance_id in instance_ids:
            for record in self.journal.list_for_instance(instance_id):
                if not self._is_planned_bound_strategy_preview(record):
                    continue
                self._invalidate_planned_record_locked(record, reason=reason)
                invalidated.append(instance_id)
        return invalidated

    @staticmethod
    def _is_planned_bound_strategy_preview(record: CampaignRecord) -> bool:
        return (
            record.status == BetaCampaignStatus.PLANNED.value
            and record.metadata.get("execution_kind") == "bound_strategy"
        )

    @classmethod
    def _is_stale_bound_strategy_preview(cls, record: CampaignRecord, strategy: VolumeStrategy) -> bool:
        if not cls._is_planned_bound_strategy_preview(record):
            return False
        return (
            record.metadata.get("strategy_id") != strategy.id
            or record.metadata.get("strategy_version") != strategy.version
        )

    def _invalidate_planned_record_locked(self, record: CampaignRecord, *, reason: str) -> None:
        invalidated_at_ms = int(time.time() * 1000)
        self.journal.update(
            record.campaign_id,
            status=BetaCampaignStatus.STOPPED.value,
            reason=reason,
            invalidated_at_ms=invalidated_at_ms,
            invalidation_reason=reason,
        )
        self._append_monitor_event(
            record,
            _sanitize_event(
                {
                    "event": "bound_strategy_preview_invalidated",
                    "reason": reason,
                    "strategy_id": record.metadata.get("strategy_id"),
                    "strategy_version": record.metadata.get("strategy_version"),
                },
            ),
        )

    def _require_record(self, instance_id: str, campaign_id: str) -> CampaignRecord:
        record = self.journal.get(campaign_id)
        if record is None or record.instance_id != instance_id:
            raise ValidationFailed("campaign was not found for this account")
        return record

    def _verify_execution_boundary(self, record: CampaignRecord, material: CredentialMaterial) -> Decimal:
        gateway: WeexGateway | None = None
        try:
            profile, gateway = self._profile_and_gateway(material)
            if live_profile_fingerprint(profile) != record.campaign.profile_fingerprint:
                raise UnsafeOperation("live profile changed since campaign preview")
            boundary = inspect_live_account(gateway, Decimal(0))
            if not _account_boundary_is_flat(boundary):
                raise UnsafeOperation("account changed after preview and is no longer flat")
            return Decimal(str(boundary["available_quote"]))
        finally:
            if gateway is not None:
                gateway.close()

    def _unresolved_uncertain(self, instance_id: str) -> CampaignRecord | None:
        return next(
            (record for record in self.journal.list_for_instance(instance_id) if _reconciliation_required(record)),
            None,
        )

    def _require_live_gate(self) -> None:
        if (
            self.settings.adapter != "weex-live"
            or not self.settings.live_campaigns_enabled
            or not self.settings.live_trading_enabled
        ):
            raise UnsafeOperation("live campaign execution is disabled")

    def _notify(self, instance_id: str) -> None:
        try:
            self.on_change(instance_id)
        except Exception:
            return


def _preview_metadata(campaign: BetaVolumeCampaign, available: Decimal, readiness: dict[str, Any]) -> dict[str, Any]:
    confirmation = campaign_confirmation(campaign)
    return {
        "confirmation": confirmation,
        "stop_confirmation": f"STOP WEEX LIVE BETA-CAMPAIGN {campaign.campaign_id.upper()} POST_ONLY",
        "available_quote": str(available),
        "required_leverage": campaign.max_auto_leverage,
        "planned_leverage": campaign.leverage if isinstance(campaign.leverage, int) else campaign.max_auto_leverage,
        "max_supported_turnover_quote": str(
            available * Decimal(campaign.max_auto_leverage) / campaign.margin_buffer * Decimal(2)
        ),
        "readiness": readiness,
        "phase": "planned",
    }


def _bound_strategy_confirmation(campaign: BetaVolumeCampaign) -> str:
    return f"EXECUTE WEEX LIVE STRATEGY {campaign.campaign_id.upper()} POST_ONLY"


def _bound_strategy_stop_confirmation(campaign_id: str) -> str:
    return f"STOP WEEX LIVE STRATEGY {campaign_id.upper()} POST_ONLY"


def _available_quote(gateway: WeexGateway) -> Decimal:
    rows = gateway.account_balance_rows("live")
    for row in rows:
        if str(row.get("asset") or "").upper() == "USDT":
            try:
                value = Decimal(str(row.get("availableBalance") or row.get("available") or "0"))
            except Exception as exc:  # noqa: BLE001
                raise ValidationFailed("WEEX available balance is invalid") from exc
            if not value.is_finite() or value < 0:
                raise ValidationFailed("WEEX available balance is invalid")
            return value
    raise ValidationFailed("WEEX account balance has no USDT row")


def _available_quote_from_readiness(readiness: Mapping[str, Any]) -> Decimal:
    """Reuse the validated balance from the just-completed account boundary check."""
    try:
        value = Decimal(str(readiness["available_quote"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise ValidationFailed("WEEX available balance is invalid") from exc
    if not value.is_finite() or value < 0:
        raise ValidationFailed("WEEX available balance is invalid")
    return value


def _normalize_proxy_url(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if "://" in text:
        return text
    return f"https://{text}"


def _reconciliation_confirmation(campaign_id: str) -> str:
    return f"RECONCILE WEEX LIVE BETA-CAMPAIGN {campaign_id.upper()} ACCOUNT_FLAT NO_ORDERS"


def _reconciliation_required(record: CampaignRecord) -> bool:
    return (
        record.status == BetaCampaignStatus.UNCERTAIN.value
        and record.metadata.get("reconciliation_acknowledged_at_ms") is None
    )


def _account_boundary_is_flat(boundary: dict[str, Any]) -> bool:
    return all(
        int(boundary.get(key, -1)) == 0
        for key in ("active_position_count", "regular_order_count", "trigger_order_count")
    )


def _campaign_result_metrics(result: dict[str, Any]) -> dict[str, Any]:
    """Project authoritative child accounting into the control-plane journal."""
    rows = result.get("children")
    children = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    if not children and isinstance(result.get("accounting"), dict):
        children = [result]
    totals = {
        "fill_count": 0,
        "maker_count": 0,
        "taker_count": 0,
        "unknown_count": 0,
        "order_count": 0,
        "cancel_count": 0,
        "requote_count": 0,
        "btc_quote": Decimal(0),
        "eth_quote": Decimal(0),
        "maker_quote": Decimal(0),
        "taker_quote": Decimal(0),
        "unknown_quote": Decimal(0),
    }
    for child in children:
        accounting = child.get("accounting")
        if isinstance(accounting, dict):
            totals["fill_count"] += _int_field(accounting, "fill_count")
            totals["maker_count"] += _int_field(accounting, "maker_count")
            totals["taker_count"] += _int_field(accounting, "taker_count")
            totals["unknown_count"] += _int_field(accounting, "unknown_liquidity_count")
            quote = _decimal_field(accounting, "executed_quote_volume")
            if bool(accounting.get("maker_only")):
                totals["maker_quote"] += quote
            elif quote:
                totals["unknown_quote"] += quote
        legs = child.get("legs")
        if isinstance(legs, list):
            for leg in legs:
                if not isinstance(leg, dict):
                    continue
                symbol = str(leg.get("symbol") or "").upper()
                quote = _decimal_field(leg, "quote_volume")
                if symbol == "BTC":
                    totals["btc_quote"] += quote
                elif symbol == "ETH":
                    totals["eth_quote"] += quote
                for key, target in (("submissions", "order_count"), ("cancels", "cancel_count")):
                    value = leg.get(key)
                    if isinstance(value, list):
                        totals[target] += len(value)
        timeline = child.get("timeline")
        if isinstance(timeline, list):
            totals["requote_count"] += sum(
                1
                for event in timeline
                if isinstance(event, dict) and "requote" in str(event.get("event") or event.get("name") or "").lower()
            )
    if not children:
        fallback_quote = _decimal_field(result, "executed_quote_volume")
        if bool(result.get("maker_only")):
            totals["maker_quote"] = fallback_quote
        elif fallback_quote:
            totals["unknown_quote"] = fallback_quote
    return {
        "fill_count": totals["fill_count"],
        "maker_count": totals["maker_count"],
        "taker_count": totals["taker_count"],
        "unknown_count": totals["unknown_count"],
        "order_count": totals["order_count"],
        "cancel_count": totals["cancel_count"],
        "requote_count": totals["requote_count"],
        "btc_quote": str(totals["btc_quote"]),
        "eth_quote": str(totals["eth_quote"]),
        "maker_quote": str(totals["maker_quote"]),
        "taker_quote": str(totals["taker_quote"]),
        "unknown_quote": str(totals["unknown_quote"]),
    }


def _int_field(payload: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(payload.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _decimal_field(payload: dict[str, Any], key: str) -> Decimal:
    try:
        value = Decimal(str(payload.get(key) or 0))
    except Exception:  # noqa: BLE001 - malformed result is reported as zero
        return Decimal(0)
    return value if value.is_finite() and value >= 0 else Decimal(0)


def _worker_exception_reason(exc: Exception) -> str:
    """Return an actionable but non-sensitive terminal worker reason.

    Exchange exceptions can embed request URLs, account identifiers, or raw
    responses.  The journal is visible in the control center, so only a small
    vocabulary of known safety conditions is persisted.  Unknown exceptions
    retain their class only and still require the existing manual reconciliation
    path.
    """
    if not isinstance(exc, SafetyError):
        return f"worker_exception:{type(exc).__name__.lower()}"
    message = str(exc).lower()
    known_codes = (
        ("available usdt", "available_balance_insufficient"),
        ("positions or orders", "account_boundary_not_flat"),
        ("flat btc/eth positions", "account_boundary_not_flat"),
        ("timing policy", "timing_policy_unavailable"),
        ("beta provider", "beta_source_unavailable"),
        ("beta moved", "beta_changed_since_preview"),
        ("authorization expired", "authorization_expired"),
        ("campaign authorization expired", "authorization_expired"),
        ("leverage", "leverage_verification_failed"),
        ("post_only", "post_only_verification_failed"),
    )
    for token, code in known_codes:
        if token in message:
            return f"worker_safety:{code}"
    return "worker_safety:preflight_rejected"


def _sanitize_event(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("event") or payload.get("name") or "event")[:96]
    timestamp_ms = payload.get("timestamp_ms")
    try:
        at_ms = int(timestamp_ms) if timestamp_ms is not None else int(time.time() * 1000)
    except (TypeError, ValueError):
        at_ms = int(time.time() * 1000)
    event: dict[str, Any] = {
        "sequence": 1,
        "name": name,
        "at_ms": at_ms,
    }
    for key in ("phase", "run", "child_plan_id", "status"):
        if payload.get(key) is not None:
            event[key] = payload[key]
    text_fields = {
        "operation",
        "reason",
        "symbol",
        "action",
        "side",
        "progress_event",
        "waiting_for",
        "source",
        "decision",
        "read",
        "child_status",
        "btc",
        "eth",
    }
    decimal_fields = {
        "remaining_quote",
        "total_quote",
        "child_quote",
        "seconds",
        "desired_quote",
        "opening_notional_quote",
        "quote_volume",
        "executed_quote_volume",
        "price",
        "quantity",
        "quote",
        "filled_quantity",
        "order_quantity",
        "remaining_quantity",
        "btc_quantity",
        "eth_quantity",
    }
    integer_fields = {
        "attempt",
        "max_attempts",
        "round",
        "event_index",
        "elapsed_ms",
        "remaining_ms",
        "next_check_ms",
        "leverage",
        "fill_count",
        "submissions",
        "cancels",
        "requotes",
    }
    boolean_fields = {
        "completed",
        "flat",
        "no_orders",
        "maker_only",
        "verified",
        "maker",
    }
    fields: dict[str, object] = {}
    if payload.get("sequence") is not None:
        with suppress(TypeError, ValueError):
            fields["leg_sequence"] = int(payload["sequence"])
    for key in text_fields:
        if payload.get(key) is not None:
            fields[key] = _safe_event_text(payload[key], limit=96)
    for key in decimal_fields:
        if payload.get(key) is None:
            continue
        try:
            value = Decimal(str(payload[key]))
        except Exception:  # noqa: BLE001 - malformed observability data is omitted
            continue
        if value.is_finite():
            fields[key] = format(value, "f")
    for key in integer_fields:
        if payload.get(key) is None:
            continue
        try:
            fields[key] = int(payload[key])
        except (TypeError, ValueError):
            continue
    for key in boolean_fields:
        if isinstance(payload.get(key), bool):
            fields[key] = payload[key]
    for key in ("symbols", "active_symbols", "completed_symbols"):
        values = payload.get(key)
        if isinstance(values, (list, tuple)):
            fields[key] = [_safe_event_text(value, limit=16) for value in values[:2]]
    if payload.get("error"):
        fields["error"] = _safe_event_text(payload["error"], limit=80)
    event["fields"] = fields
    event["message"] = name.replace("_", " ")[:240]
    return event


_SAFE_EVENT_TEXT = re.compile(r"[^A-Za-z0-9._:/+\- ]+")


def _safe_event_text(value: object, *, limit: int) -> str:
    return _SAFE_EVENT_TEXT.sub("", str(value)).strip()[:limit]


def _phase_for_event(name: str) -> str:
    if name.startswith("safe_stop"):
        return "safe_stop"
    if "planning" in name:
        return "planning"
    if "run_started" in name:
        return "opening"
    if "run_completed" in name:
        return "reconciled"
    if "boundary" in name:
        return "boundary"
    if "finished" in name:
        return "finished"
    if "retry" in name:
        return "recovery"
    return name[:64]


def _publishes_fleet_snapshot(name: str) -> bool:
    return name in {
        "campaign_boundary_completed",
        "campaign_child_planning_completed",
        "campaign_run_started",
        "campaign_run_completed",
        "preflight_completed",
        "preflight_rejected",
        "cycle_started",
        "cycle_completed",
        "cycle_stopped",
        # These are low-frequency, fill-reconciled state changes.  Publishing
        # them makes the account table follow the same durable monitor
        # projection without broadcasting the 125ms maker-wait heartbeats.
        "leg_completed",
        "leg_stopped",
        "leg_uncertain",
        "hold_started",
        "hold_completed",
        "round_gap_started",
        "round_gap_completed",
        "final_acceptance_completed",
        "workflow_finished",
        "campaign_finished",
        "campaign_uncertain",
    }


def _view(record: CampaignRecord | None, *, include_events: bool = True) -> BetaCampaignView:
    if record is None:
        raise ValidationFailed("campaign was not found")
    campaign = record.campaign
    metadata = record.metadata
    result = record.result or {}
    generated = Decimal(str(metadata.get("generated_quote", result.get("executed_quote_volume", "0"))))
    remaining = Decimal(
        str(
            metadata.get(
                "remaining_quote",
                result.get("remaining_quote", max(Decimal(0), campaign.target_turnover_quote - generated)),
            )
        )
    )
    excess = Decimal(
        str(
            metadata.get(
                "excess_quote", result.get("excess_quote", max(Decimal(0), generated - campaign.target_turnover_quote))
            )
        )
    )
    started = metadata.get("started_at_ms")
    finished = metadata.get("finished_at_ms")
    return BetaCampaignView(
        campaign_id=campaign.campaign_id,
        instance_id=record.instance_id,
        status=record.status,
        schema_version=campaign.schema_version,
        strategy_id=str(metadata["strategy_id"]) if metadata.get("strategy_id") else None,
        strategy_name=str(metadata["strategy_name"]) if metadata.get("strategy_name") else None,
        strategy_version=int(metadata["strategy_version"]) if metadata.get("strategy_version") is not None else None,
        strategy_snapshot=dict(metadata["strategy_snapshot"])
        if isinstance(metadata.get("strategy_snapshot"), dict)
        else None,
        session_id=str(metadata["session_id"]) if metadata.get("session_id") else None,
        target_mode=str(metadata["target_mode"]) if metadata.get("target_mode") else None,
        run_disposition=str(metadata["run_disposition"]) if metadata.get("run_disposition") else None,
        strategy_target_quote_volume=(
            Decimal(str(metadata["strategy_target_quote"]))
            if metadata.get("strategy_target_quote") is not None
            else None
        ),
        execution_target_quote_volume=(
            Decimal(str(metadata["session_target_quote"])) if metadata.get("session_target_quote") is not None else None
        ),
        baseline_lifetime_quote_volume=(
            Decimal(str(metadata["baseline_lifetime_quote"]))
            if metadata.get("baseline_lifetime_quote") is not None
            else None
        ),
        target_quote=campaign.target_turnover_quote,
        round_turnover_quote_min=campaign.round_turnover_quote_min,
        cycle_volume=campaign.round_turnover_quote,
        authorized_max_quote=campaign.authorized_max_turnover_quote,
        hold_min_seconds=int(campaign.hold_min_seconds),
        hold_max_seconds=int(campaign.hold_max_seconds),
        round_gap_min_seconds=int(campaign.round_gap_min_seconds),
        round_gap_max_seconds=int(campaign.round_gap_max_seconds),
        max_runs=campaign.max_runs,
        beta=campaign.allocation.beta,
        beta_version=campaign.allocation.version,
        beta_source=campaign.allocation.source,
        beta_as_of_ms=campaign.allocation.as_of_ms,
        beta_age_ms=Decimal(max(0, int(time.time() * 1000) - campaign.allocation.as_of_ms)),
        beta_max_age_ms=Decimal("10000"),
        btc_long_weight=campaign.allocation.btc_long_weight,
        eth_short_weight=campaign.allocation.eth_short_weight,
        available_quote=Decimal(str(metadata["available_quote"]))
        if metadata.get("available_quote") is not None
        else None,
        required_leverage=int(metadata["required_leverage"]) if metadata.get("required_leverage") is not None else None,
        planned_leverage=int(metadata["planned_leverage"]) if metadata.get("planned_leverage") is not None else None,
        max_supported_turnover_quote=Decimal(str(metadata["max_supported_turnover_quote"]))
        if metadata.get("max_supported_turnover_quote")
        else None,
        confirmation=str(metadata["confirmation"]),
        stop_confirmation=str(metadata["stop_confirmation"]),
        reconciliation_confirmation=(
            _reconciliation_confirmation(campaign.campaign_id) if _reconciliation_required(record) else None
        ),
        reconciliation_required=_reconciliation_required(record),
        retry_allowed=False,
        risk_acknowledged=bool(metadata.get("risk_acknowledged", False)),
        current_run=int(metadata.get("current_run", 0)),
        generated_quote=generated,
        remaining_quote=remaining,
        excess_quote=excess,
        maker_quote=Decimal(
            str(
                metadata.get(
                    "maker_quote", result.get("executed_quote_volume", "0") if result.get("maker_only") else "0"
                )
            )
        ),
        taker_quote=Decimal(str(metadata.get("taker_quote", "0"))),
        unknown_quote=Decimal(str(metadata.get("unknown_quote", "0"))),
        btc_quote=Decimal(str(metadata.get("btc_quote", "0"))),
        eth_quote=Decimal(str(metadata.get("eth_quote", "0"))),
        fill_count=int(metadata.get("fill_count", 0)),
        maker_count=int(metadata.get("maker_count", 0)),
        taker_count=int(metadata.get("taker_count", 0)),
        unknown_count=int(metadata.get("unknown_count", 0)),
        order_count=int(metadata.get("order_count", 0)),
        cancel_count=int(metadata.get("cancel_count", 0)),
        requote_count=int(metadata.get("requote_count", 0)),
        phase=str(metadata.get("phase", record.status)),
        reason=str(metadata["reason"]) if metadata.get("reason") else None,
        started_at_ms=int(started) if started else None,
        finished_at_ms=int(finished) if finished else None,
        elapsed_ms=(int(finished) - int(started)) if started and finished else None,
        last_event=BetaCampaignEvent.model_validate(record.events[-1]) if record.events else None,
        events=[BetaCampaignEvent.model_validate(event) for event in record.events] if include_events else [],
    )

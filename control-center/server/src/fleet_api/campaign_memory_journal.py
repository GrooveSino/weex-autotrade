from __future__ import annotations

import json
import time
from dataclasses import replace
from threading import RLock
from typing import Any

from weex_cli.beta_campaign import BetaVolumeCampaign
from weex_cli.execution_progress import ExecutionProgressProjector

from .campaign_contracts import ACTIVE_STATUSES, CampaignRecord, ExecutionMonitorProjection
from .models import BetaCampaignStatus
from .service import UnsafeOperation

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
                        record.campaign_id, status=BetaCampaignStatus.RECOVERING.value, reason="control_plane_restart"
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

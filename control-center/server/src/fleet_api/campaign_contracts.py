from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from weex_cli.beta_campaign import BetaVolumeCampaign

from .models import BetaCampaignStatus
from .service import UnsafeOperation


ACTIVE_STATUSES = {
    BetaCampaignStatus.PLANNED.value,
    BetaCampaignStatus.EXECUTING.value,
    BetaCampaignStatus.STOPPING.value,
}

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

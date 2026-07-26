"""Atomic persistence for live Maker volume plans and execution checkpoints."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from weex_cli.core.errors import SafetyError, ValidationError

from .contracts import DEFAULT_PLAN_DIRECTORY, LiveMakerVolumePlan


@dataclass(frozen=True)
class LiveMakerVolumeRecord:
    plan: LiveMakerVolumePlan
    state: str
    result: Any = None


class LiveMakerVolumePlanStore:
    def __init__(self, directory: Path = DEFAULT_PLAN_DIRECTORY) -> None:
        self.directory = directory

    def create(self, plan: LiveMakerVolumePlan) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(plan.plan_id)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            raise SafetyError(f"live Maker volume plan already exists: {plan.plan_id}") from None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(record_payload(plan, "planned", None), handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def save(self, plan: LiveMakerVolumePlan, *, state: str, result: Any) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(plan.plan_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(record_payload(plan, state, result), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return path

    def load_record(self, plan_id: str) -> LiveMakerVolumeRecord:
        path = self._path(plan_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValidationError(f"live Maker volume plan not found: {plan_id}") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"cannot read live Maker volume plan: {plan_id}") from exc
        if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
            raise ValidationError("stored live Maker volume plan is invalid")
        plan_payload = payload.get("plan")
        if not isinstance(plan_payload, Mapping):
            raise ValidationError("stored live Maker volume plan has no plan payload")
        return LiveMakerVolumeRecord(
            plan=LiveMakerVolumePlan.from_dict(plan_payload),
            state=str(payload.get("state") or "unknown"),
            result=payload.get("result"),
        )

    def load(self, plan_id: str) -> LiveMakerVolumePlan:
        return self.load_record(plan_id).plan

    def claim_for_execution(self, plan: LiveMakerVolumePlan) -> None:
        record = self.load_record(plan.plan_id)
        if record.state != "planned":
            raise SafetyError(f"live Maker volume plan is already {record.state}; create a new dry run")
        self.save(plan, state="executing", result={"status": "executing", "reason": "claimed"})

    def _path(self, plan_id: str) -> Path:
        if not plan_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in plan_id):
            raise ValidationError("invalid live Maker volume plan ID")
        return self.directory / f"{plan_id}.json"


def record_payload(plan: LiveMakerVolumePlan, state: str, result: Any) -> dict[str, Any]:
    return {"schema_version": 1, "state": state, "plan": plan.as_dict(), "result": result}

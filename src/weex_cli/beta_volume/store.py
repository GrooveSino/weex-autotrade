from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from weex_cli.core.errors import SafetyError, ValidationError

from .contracts import DEFAULT_PLAN_DIRECTORY
from .plan import BetaVolumePlan


class BetaVolumePlanStore:
    def __init__(self, directory: Path = DEFAULT_PLAN_DIRECTORY) -> None:
        self.directory = directory

    def save(self, plan: BetaVolumePlan, *, state: str = "planned", result: Any = None) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{plan.plan_id}.json"
        temporary = path.with_suffix(".tmp")
        payload = {"schema_version": plan.schema_version, "state": state, "plan": plan.as_dict(), "result": result}
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
        return path

    def create(self, plan: BetaVolumePlan) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{plan.plan_id}.json"
        payload = {"schema_version": plan.schema_version, "state": "planned", "plan": plan.as_dict(), "result": None}
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            raise SafetyError(f"Beta plan already exists: {plan.plan_id}") from None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path

    def claim_for_execution(self, plan: BetaVolumePlan) -> None:
        record = self.load_record(plan.plan_id)
        if record.plan != plan or record.state != "planned":
            raise SafetyError(
                f"plan {plan.plan_id} is not in a pristine planned state; inspect live state before recovery"
            )
        claim_path = self.directory / f"{plan.plan_id}.claim"
        try:
            descriptor = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SafetyError(f"plan {plan.plan_id} is already claimed or consumed") from None
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(str(int(time.time() * 1000)))
                handle.flush()
                os.fsync(handle.fileno())
            current = self.load_record(plan.plan_id)
            if current.plan != plan or current.state != "planned":
                claim_path.unlink(missing_ok=True)
                raise SafetyError(f"plan {plan.plan_id} changed before it could be claimed")
            self.save(plan, state="executing")
        except Exception:
            # A failed state transition remains claimed unless it was proven not to have started.
            raise

    def claim_for_recovery(self, plan: BetaVolumePlan, symbol: str | None = None) -> None:
        record = self.load_record(plan.plan_id)
        if record.state not in {"uncertain", "stopped", "recovery_uncertain"}:
            raise SafetyError(f"plan {plan.plan_id} is not in a recoverable state")
        suffix = f".{symbol.strip().lower()}" if symbol else ""
        claim_path = self.directory / f"{plan.plan_id}{suffix}.recovery.claim"
        try:
            descriptor = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SafetyError(f"recovery for plan {plan.plan_id} is already claimed") from None
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(str(int(time.time() * 1000)))
            handle.flush()
            os.fsync(handle.fileno())

    def save_recovery(self, plan: BetaVolumePlan, result: Any, symbol: str | None = None) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        suffix = f".{symbol.strip().lower()}" if symbol else ""
        path = self.directory / f"{plan.plan_id}{suffix}.recovery.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
        return path

    def claim_market_close_intent(self, plan: BetaVolumePlan, key: str, *, created_at_ms: int) -> bool:
        """Persist the one-shot market-close boundary before calling WEEX."""
        self.directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        path = self.directory / f"{plan.plan_id}.{digest}.market-close.intent"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(f"{created_at_ms}\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True

    def load(self, plan_id: str) -> tuple[BetaVolumePlan, str]:
        record = self.load_record(plan_id)
        return record.plan, record.state

    def load_record(self, plan_id: str) -> BetaVolumePlanRecord:
        plan_id = plan_id.lower()
        if not plan_id.startswith("wv-") or not plan_id[3:].isalnum():
            raise ValidationError("invalid Beta plan ID")
        path = self.directory / f"{plan_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValidationError(f"Beta plan not found: {plan_id}") from None
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            raise ValidationError(f"Beta plan is unreadable: {plan_id}") from None
        if not isinstance(payload, Mapping) or payload.get("schema_version") not in {1, 2, 3, 4, 5}:
            raise ValidationError("stored Beta plan schema is invalid")
        plan_row = payload.get("plan")
        if not isinstance(plan_row, Mapping):
            raise ValidationError("stored Beta plan payload is invalid")
        return BetaVolumePlanRecord(
            plan=BetaVolumePlan.from_dict(plan_row),
            state=str(payload.get("state") or "unknown"),
            result=payload.get("result"),
        )


@dataclass(frozen=True)
class BetaVolumePlanRecord:
    plan: BetaVolumePlan
    state: str
    result: Any = None

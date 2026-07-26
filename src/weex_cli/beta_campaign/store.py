"""Filesystem persistence for immutable Campaign authorization records."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from weex_cli.core.errors import SafetyError, ValidationError

from .model import DEFAULT_CAMPAIGN_DIRECTORY, BetaVolumeCampaign


@dataclass(frozen=True)
class BetaVolumeCampaignRecord:
    campaign: BetaVolumeCampaign
    state: str
    result: Any = None


class BetaVolumeCampaignStore:
    def __init__(self, directory: Path = DEFAULT_CAMPAIGN_DIRECTORY) -> None:
        self.directory = directory

    def create(self, campaign: BetaVolumeCampaign) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{campaign.campaign_id}.json"
        payload = {
            "schema_version": campaign.schema_version,
            "state": "planned",
            "campaign": campaign.as_dict(),
            "result": None,
        }
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SafetyError(f"campaign already exists: {campaign.campaign_id}") from None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path

    def save(self, campaign: BetaVolumeCampaign, *, state: str, result: Any) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{campaign.campaign_id}.json"
        temporary = path.with_suffix(".tmp")
        payload = {
            "schema_version": campaign.schema_version,
            "state": state,
            "campaign": campaign.as_dict(),
            "result": result,
        }
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
        return path

    def claim_for_execution(self, campaign: BetaVolumeCampaign) -> None:
        record = self.load(campaign.campaign_id)
        if record.campaign != campaign or record.state != "planned":
            raise SafetyError("campaign is not in a pristine planned state")
        claim_path = self.directory / f"{campaign.campaign_id}.claim"
        try:
            descriptor = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SafetyError("campaign is already claimed or consumed") from None
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(str(int(time.time() * 1000)))
            handle.flush()
            os.fsync(handle.fileno())
        self.save(campaign, state="executing", result=None)

    def load(self, campaign_id: str) -> BetaVolumeCampaignRecord:
        normalized = campaign_id.lower()
        if not normalized.startswith("wc-") or not normalized[3:].isalnum():
            raise ValidationError("invalid campaign ID")
        path = self.directory / f"{normalized}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValidationError(f"campaign not found: {normalized}") from None
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            raise ValidationError(f"campaign is unreadable: {normalized}") from None
        if not isinstance(payload, Mapping):
            raise ValidationError("stored campaign schema is invalid")
        campaign_row = payload.get("campaign")
        if payload.get("schema_version") not in {1, 2, 3, 4, 5} or not isinstance(campaign_row, Mapping):
            raise ValidationError("stored campaign schema is invalid")
        return BetaVolumeCampaignRecord(
            campaign=BetaVolumeCampaign.from_dict(campaign_row),
            state=str(payload.get("state") or "unknown"),
            result=payload.get("result"),
        )

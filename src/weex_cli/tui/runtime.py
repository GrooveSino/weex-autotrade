from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from weex_cli.beta_campaign import BetaVolumeCampaignRecord, BetaVolumeCampaignStore
from weex_cli.beta_campaign.workflow import CampaignRuntimePaths
from weex_cli.core.errors import SafetyError
from weex_cli.core.redaction import redact


class TuiCampaignJournal:
    def __init__(self, paths: CampaignRuntimePaths) -> None:
        self.paths = paths
        self.store = BetaVolumeCampaignStore(paths.campaigns)
        self._lock = threading.Lock()

    def append_event(self, campaign_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
        safe = redact(dict(event))
        if not isinstance(safe, dict):
            safe = {"event": "invalid_event"}
        stored = {"timestamp_ms": int(time.time() * 1000), **safe}
        path = self._event_path(campaign_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(stored, separators=(",", ":"), sort_keys=True) + "\n")
                handle.flush()
        return stored

    def events(self, campaign_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        path = self._event_path(campaign_id)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        rows: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    def unresolved_uncertain(self) -> list[BetaVolumeCampaignRecord]:
        records: list[BetaVolumeCampaignRecord] = []
        if not self.paths.campaigns.exists():
            return records
        for path in sorted(self.paths.campaigns.glob("wc-*.json")):
            try:
                record = self.store.load(path.stem)
            except Exception as exc:  # noqa: BLE001 - unreadable journals must fail closed
                raise SafetyError("campaign journal is unreadable; manual inspection is required") from exc
            if record.state == "uncertain" and not self._ack_path(record.campaign.campaign_id).is_file():
                records.append(record)
        return records

    def acknowledge_reconciliation(self, campaign_id: str, confirmation: str) -> Path:
        record = self.store.load(campaign_id)
        if record.state != "uncertain":
            raise SafetyError("manual reconciliation is only available for an uncertain campaign")
        expected = reconciliation_confirmation(record.campaign.campaign_id)
        if confirmation != expected:
            raise SafetyError("manual reconciliation confirmation does not match exactly")
        path = self._ack_path(record.campaign.campaign_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": 1,
                    "campaign_id": record.campaign.campaign_id,
                    "acknowledged_at_ms": int(time.time() * 1000),
                    "exchange_boundary": "btc_eth_flat_no_regular_or_trigger_orders",
                },
                handle,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def _event_path(self, campaign_id: str) -> Path:
        return self.paths.campaigns / f"{campaign_id.lower()}.events.jsonl"

    def _ack_path(self, campaign_id: str) -> Path:
        return self.paths.campaigns / "reconciliations" / f"{campaign_id.lower()}.json"


def reconciliation_confirmation(campaign_id: str) -> str:
    return f"RECONCILE WEEX LIVE BETA-CAMPAIGN {campaign_id.upper()} ACCOUNT_FLAT NO_ORDERS"


def boundary_is_flat(snapshot: Mapping[str, Any]) -> bool:
    return all(
        int(snapshot.get(key, -1)) == 0
        for key in ("active_position_count", "regular_order_count", "trigger_order_count")
    )

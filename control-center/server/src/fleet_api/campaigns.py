from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from weex_cli.beta_allocation import HttpBetaAllocationProvider
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
)
from .service import UnsafeOperation, ValidationFailed
from .vault import CredentialMaterial, CredentialVault


class CampaignJournal(Protocol):
    def create(self, instance_id: str, campaign: BetaVolumeCampaign, metadata: dict[str, Any]) -> None: ...

    def get(self, campaign_id: str) -> CampaignRecord | None: ...

    def list_for_instance(self, instance_id: str) -> list[CampaignRecord]: ...

    def list_all(self) -> list[CampaignRecord]: ...

    def active_for_instance(self, instance_id: str) -> CampaignRecord | None: ...

    def update(
        self, campaign_id: str, *, status: str | None = None, result: dict[str, Any] | None = None, **metadata: Any
    ) -> None: ...

    def add_event(self, campaign_id: str, event: dict[str, Any]) -> int: ...

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


ACTIVE_STATUSES = {
    BetaCampaignStatus.PLANNED.value,
    BetaCampaignStatus.EXECUTING.value,
    BetaCampaignStatus.STOPPING.value,
}


class InMemoryCampaignJournal:
    def __init__(self) -> None:
        self._records: dict[str, CampaignRecord] = {}
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
            """
        )
        self._connection.commit()
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
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM beta_campaign_events WHERE campaign_id = ?",
                (campaign_id.lower(),),
            ).fetchone()
            sequence = int(row[0]) + 1
            stored_event = {**event, "sequence": sequence}
            self._connection.execute(
                "INSERT INTO beta_campaign_events(campaign_id, sequence, payload, created_at_ms) VALUES (?, ?, ?, ?)",
                (
                    campaign_id.lower(),
                    sequence,
                    json.dumps(stored_event, separators=(",", ":")),
                    int(time.time() * 1000),
                ),
            )
            return sequence

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


class CampaignWorkerManager:
    def __init__(
        self,
        settings: ControlPlaneSettings,
        vault: CredentialVault,
        journal: CampaignJournal,
        beta_provider_factory: Callable[[], HttpBetaAllocationProvider],
        *,
        on_change: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.vault = vault
        self.journal = journal
        self.beta_provider_factory = beta_provider_factory
        self.on_change = on_change or (lambda _instance_id: None)
        self._executor = ThreadPoolExecutor(
            max_workers=settings.live_campaign_worker_count, thread_name_prefix="weex-campaign"
        )
        self._stops: dict[str, threading.Event] = {}
        self._futures: dict[str, Future[None]] = {}
        self._lock = RLock()

    def recover(self) -> int:
        count = self.journal.recover_incomplete()
        for record in self.journal.list_all():
            self._notify(record.instance_id)
        return count

    def preview(
        self, instance_id: str, request: BetaCampaignPreviewRequest, material: CredentialMaterial | None
    ) -> BetaCampaignPreview:
        self._require_live_gate()
        if material is None:
            raise UnsafeOperation("account credentials are unavailable")
        if self.journal.active_for_instance(instance_id) is not None:
            raise UnsafeOperation("this account already has an active Beta Campaign")
        profile, gateway = self._profile_and_gateway(material)
        provider = self.beta_provider_factory()
        try:
            allocation = provider.get()
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
            available = _available_quote(gateway)
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
            self.journal.create(instance_id, campaign, metadata)
            BetaVolumeCampaignStore(self.settings.campaign_data_directory / instance_id).create(campaign)
            return _view(self.journal.get(campaign.campaign_id), include_events=False)  # type: ignore[arg-type]
        finally:
            gateway.close()

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
        if record.campaign.schema_version != 2:
            raise UnsafeOperation("campaign schema is not executable; create a new Beta Campaign")
        if int(time.time() * 1000) >= record.campaign.expires_at_ms:
            raise UnsafeOperation("campaign authorization has expired")
        if confirmation != str(record.metadata["confirmation"]):
            raise UnsafeOperation("exact campaign confirmation does not match")
        if material is None:
            raise UnsafeOperation("account credentials are unavailable")
        self._verify_profile_fingerprint(record, material)
        stop = threading.Event()
        with self._lock:
            self._stops[campaign_id] = stop
            self.journal.update(
                campaign_id,
                status=BetaCampaignStatus.EXECUTING.value,
                risk_acknowledged=True,
                started_at_ms=int(time.time() * 1000),
            )
            future = self._executor.submit(self._run, record, material, stop)
            self._futures[campaign_id] = future
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

    def public_snapshot(self) -> list[dict[str, Any]]:
        if not hasattr(self.journal, "list_all"):
            return []
        return [
            _view(record, include_events=False).model_dump(mode="json", by_alias=True)
            for record in self.journal.list_all()
        ]  # type: ignore[attr-defined]

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)
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
            sequence = self.journal.add_event(campaign_id, event)
            event["sequence"] = sequence
            metadata: dict[str, Any] = {"phase": _phase_for_event(str(event["name"]))}
            if event.get("run") is not None:
                metadata["current_run"] = event["run"]
            self.journal.update(campaign_id, **metadata)
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
                **metrics,
            )
        except Exception as exc:  # noqa: BLE001 - a worker failure is an uncertain live outcome
            self.journal.update(
                campaign_id,
                status=BetaCampaignStatus.UNCERTAIN.value,
                finished_at_ms=int(time.time() * 1000),
                reason=f"worker_exception:{type(exc).__name__.lower()}",
            )
            event = _sanitize_event({"event": "campaign_uncertain", "error": type(exc).__name__})
            event["sequence"] = self.journal.add_event(campaign_id, event)
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
            proxy_url=_normalize_proxy_url(material.proxy_url.get_secret_value()),
            allow_live_mutations=True,
            post_only_only=True,
        )
        profile.require_maker_execution()
        return profile, WeexGateway(settings, proxy_url=profile.proxy_url)

    def _require_record(self, instance_id: str, campaign_id: str) -> CampaignRecord:
        record = self.journal.get(campaign_id)
        if record is None or record.instance_id != instance_id:
            raise ValidationFailed("campaign was not found for this account")
        return record

    def _verify_profile_fingerprint(self, record: CampaignRecord, material: CredentialMaterial) -> None:
        gateway: WeexGateway | None = None
        try:
            profile, gateway = self._profile_and_gateway(material)
            if live_profile_fingerprint(profile) != record.campaign.profile_fingerprint:
                raise UnsafeOperation("live profile changed since campaign preview")
        finally:
            if gateway is not None:
                gateway.close()

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


def _normalize_proxy_url(value: str) -> str:
    text = value.strip()
    if "://" in text:
        return text
    return f"https://{text}"


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


def _sanitize_event(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("event") or payload.get("name") or "event")[:96]
    event: dict[str, Any] = {
        "sequence": int(payload.get("sequence") or 1),
        "name": name,
        "at_ms": int(time.time() * 1000),
    }
    for key in ("phase", "run", "child_plan_id", "status"):
        if payload.get(key) is not None:
            event[key] = payload[key]
    allowed = {
        "remaining_quote",
        "total_quote",
        "child_quote",
        "attempt",
        "max_attempts",
        "seconds",
        "operation",
        "reason",
    }
    fields = {
        key: payload[key] for key in allowed if key in payload and isinstance(payload[key], (str, int, float, bool))
    }
    if payload.get("error"):
        fields["error"] = str(payload["error"])[:80]
    event["fields"] = fields
    event["message"] = name.replace("_", " ")[:240]
    return event


def _phase_for_event(name: str) -> str:
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
        target_quote=campaign.target_turnover_quote,
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

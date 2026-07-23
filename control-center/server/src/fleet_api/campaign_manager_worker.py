from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from weex_cli.beta_campaign import BetaVolumeCampaignStore, inspect_live_account, live_profile_fingerprint
from weex_cli.beta_volume import BetaVolumePlanStore
from weex_cli.config import Credentials, Settings
from weex_cli.execution_progress import EXECUTION_PROGRESS_PROJECTION_VERSION
from weex_cli.gateway import WeexGateway
from weex_cli.live_profile import LiveProfile
from weex_cli.live_websocket import WeexCampaignWebSocketRuntime

from .campaign_contracts import CampaignRecord
from .campaign_events import _publishes_fleet_snapshot, _sanitize_event
from .campaign_helpers import (
    _available_quote,
    _account_boundary_is_flat,
    _campaign_result_metrics,
    _normalize_proxy_url,
    _reconciliation_required,
    _worker_exception_reason,
)
from .campaign_recovery import acknowledge_recovered_uncertain, recover_uncertain_before_preview
from .models import BetaCampaignStatus
from .ownership import LEGACY_OWNER_USER_ID
from .service import UnsafeOperation, ValidationFailed
from .vault import CredentialMaterial

class CampaignWorkerRuntimeMixin:
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
            # Resolve these two runtime collaborators through the legacy facade.
            # Existing integrations patch that public seam, while the worker stays
            # isolated from the facade for every other dependency.
            from . import campaigns as campaign_api

            websocket_runtime = campaign_api.WeexCampaignWebSocketRuntime(
                snapshot_gateway,
                profile.settings.require_credentials(),
                proxy_url=profile.proxy_url,
            )
            websocket_runtime.start()
            result = campaign_api.LiveBetaVolumeCampaignService(
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

    def _recover_uncertain_before_preview(self, instance_id: str, material: CredentialMaterial) -> None:
        recover_uncertain_before_preview(
            self.journal,
            instance_id,
            material,
            profile_and_gateway=self._profile_and_gateway,
            reconciliation_required=_reconciliation_required,
            account_boundary_is_flat=_account_boundary_is_flat,
            append_monitor_event=self._append_monitor_event,
            sanitize_event=_sanitize_event,
            notify=self._notify,
        )

    def _acknowledge_recovered_uncertain(
        self,
        record: CampaignRecord,
        material: CredentialMaterial,
        *,
        source: str,
    ) -> None:
        acknowledge_recovered_uncertain(
            self.journal,
            record,
            material,
            source=source,
            profile_and_gateway=self._profile_and_gateway,
            account_boundary_is_flat=_account_boundary_is_flat,
            append_monitor_event=self._append_monitor_event,
            sanitize_event=_sanitize_event,
            notify=self._notify,
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

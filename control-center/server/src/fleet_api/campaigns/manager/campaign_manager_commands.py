"""Preview, admission, and stop commands for Campaign workers."""

from __future__ import annotations

import threading
import time
from decimal import Decimal

from weex_cli.beta_allocation import BetaUnavailable
from weex_cli.beta_campaign import (
    BetaVolumeCampaign,
    BetaVolumeCampaignStore,
    inspect_live_account,
    live_profile_fingerprint,
)

from fleet_api.auth.ownership import LEGACY_OWNER_USER_ID
from fleet_api.auth.vault import CredentialMaterial
from fleet_api.campaigns.core.campaign_contracts import CampaignRecord, _AccountLease
from fleet_api.campaigns.core.campaign_events import _sanitize_event, _view
from fleet_api.campaigns.core.campaign_helpers import _available_quote_from_readiness, _preview_metadata
from fleet_api.models import BetaCampaignPreview, BetaCampaignPreviewRequest, BetaCampaignStatus, BetaCampaignView
from fleet_api.services.control.service import BetaSourceUnavailable, UnsafeOperation


class CampaignCommandMixin:
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
        self._recover_uncertain_before_preview(instance_id, material)
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
            boundary_counts = ("active_position_count", "regular_order_count", "trigger_order_count")
            if any(readiness.get(key, 0) for key in boundary_counts):
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
        if record.campaign.schema_version not in {2, 3, 4, 5}:
            raise UnsafeOperation("execution schema is not executable; create a new preview")
        with self._lock:
            if self._closing:
                raise UnsafeOperation("campaign manager is shutting down")
            if campaign_id in self._starting or campaign_id in self._futures:
                raise UnsafeOperation("campaign is already starting or running")
            if not self.capacity.admit(campaign_id):
                raise UnsafeOperation(
                    f"Fleet active execution capacity is full ({self.capacity.snapshot().max_active_executions})"
                )
            self._starting.add(campaign_id)
        lease: _AccountLease | None = None
        submitted = False
        try:
            if int(time.time() * 1000) >= record.campaign.expires_at_ms:
                self.journal.update(
                    campaign_id,
                    status=BetaCampaignStatus.STOPPED.value,
                    finished_at_ms=int(time.time() * 1000),
                    reason="launch_aborted:authorization_expired",
                )
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
            try:
                starting_available_balance = self._verify_execution_boundary(record, material)
            except Exception as exc:
                reason = f"launch_aborted:execution_boundary:{type(exc).__name__.lower()}"
                self.journal.update(
                    campaign_id,
                    status=BetaCampaignStatus.STOPPED.value,
                    finished_at_ms=int(time.time() * 1000),
                    reason=reason,
                )
                event = _sanitize_event({"event": "launch_aborted", "reason": reason})
                event["sequence"] = self._append_monitor_event(record, event)
                self._notify(instance_id)
                raise UnsafeOperation("启动条件已变化，请重新确认") from exc
            self.journal.update(campaign_id, starting_available_balance_quote=str(starting_available_balance))
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
                        status=BetaCampaignStatus.STOPPED.value,
                        finished_at_ms=int(time.time() * 1000),
                        reason=f"launch_aborted:execution_claim_callback_failed:{type(exc).__name__.lower()}",
                    )
                    raise UnsafeOperation("execution could not establish its local ledger session") from exc
                self._stops[campaign_id] = stop
                self._leases[campaign_id] = lease
                claimed = self._require_record(instance_id, campaign_id)
                try:
                    if self.settings.async_actor_runtime_enabled:
                        self._start_actor(claimed, material, stop)
                    else:
                        future = self._executor.submit(self._run, claimed, material, stop)
                        self._futures[campaign_id] = future
                except Exception as exc:
                    self._stops.pop(campaign_id, None)
                    self._leases.pop(campaign_id, None)
                    self.journal.update(
                        campaign_id,
                        status=BetaCampaignStatus.STOPPED.value,
                        finished_at_ms=int(time.time() * 1000),
                        reason=f"launch_aborted:worker_submit_failed:{type(exc).__name__.lower()}",
                    )
                    raise UnsafeOperation("campaign worker could not be started; start can be prepared again") from exc
                submitted = True
        finally:
            with self._lock:
                self._starting.discard(campaign_id)
            if lease is not None and not submitted:
                lease.release()
            if not submitted:
                self.capacity.release_execution(campaign_id)
                self._notify(instance_id)
        self._notify(instance_id)
        return _view(self.journal.get(campaign_id), include_events=False)  # type: ignore[arg-type]

    def stop(
        self,
        instance_id: str,
        campaign_id: str,
        confirmation: str,
        material: CredentialMaterial | None = None,
    ) -> BetaCampaignView:
        record = self._require_record(instance_id, campaign_id)
        if record.status not in {
            BetaCampaignStatus.EXECUTING.value,
            BetaCampaignStatus.STOPPING.value,
            BetaCampaignStatus.RECOVERING.value,
        }:
            raise UnsafeOperation("campaign is not running")
        if confirmation != str(record.metadata["stop_confirmation"]):
            raise UnsafeOperation("exact stop confirmation does not match")
        with self._lock:
            event = self._stops.get(campaign_id)
            if event is None and record.status != BetaCampaignStatus.RECOVERING.value:
                raise UnsafeOperation("campaign worker is not available")
            if event is not None:
                event.set()
                if self.settings.async_actor_runtime_enabled:
                    self._actor_runtime.stop(campaign_id)
        if record.status == BetaCampaignStatus.RECOVERING.value:
            self._start_recovery_stop(record, material)
        self.journal.update(campaign_id, status=BetaCampaignStatus.STOPPING.value, reason="stop_requested")
        self._notify(instance_id)
        return _view(self.journal.get(campaign_id), include_events=False)  # type: ignore[arg-type]

    def _start_recovery_stop(self, record: CampaignRecord, material: CredentialMaterial | None) -> None:
        if material is None:
            raise UnsafeOperation("账号凭据不可用，无法执行安全收尾")
        if str(record.metadata.get("recovery_boundary_state") or "") != "owned_exposure":
            raise UnsafeOperation("当前仓位无法证明属于本次任务，系统不会自动平仓")
        if not self.capacity.admit(record.campaign_id):
            raise UnsafeOperation("执行器当前没有可用的恢复容量")
        stop = threading.Event()
        lease = _AccountLease(
            self.settings.campaign_data_directory,
            material.api_key.get_secret_value(),
            record.instance_id,
            record.campaign_id,
        )
        try:
            lease.acquire()
            with self._lock:
                self._stops[record.campaign_id] = stop
                self._leases[record.campaign_id] = lease
            self._start_recovery_actor(record, material, stop)
        except Exception as exc:
            with self._lock:
                self._stops.pop(record.campaign_id, None)
                self._leases.pop(record.campaign_id, None)
            lease.release()
            self.capacity.release_execution(record.campaign_id)
            raise UnsafeOperation(f"安全收尾无法启动：{type(exc).__name__}；未提交新的平仓命令") from None

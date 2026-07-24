from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from weex_cli.beta_campaign import BetaVolumeCampaignStore, inspect_live_account, live_profile_fingerprint
from weex_cli.beta_volume import BetaVolumePlanStore
from weex_cli.gateway import WeexGateway
from weex_cli.live_profile import LiveProfile
from weex_cli.live_websocket import WeexCampaignWebSocketRuntime

from .campaign_contracts import CampaignRecord
from .campaign_events import _sanitize_event, submission_attempted
from .campaign_helpers import (
    _account_boundary_is_flat,
    _available_quote,
    _available_quote_from_readiness,
    _campaign_result_metrics,
    _reconciliation_required,
    _worker_exception_reason,
)
from .campaign_monitor_publish import append_monitor_event_direct, publish_monitor_event
from .campaign_profile import build_live_profile_gateway
from .campaign_recovery import acknowledge_recovered_uncertain, recover_uncertain_before_preview
from .execution_io import BoundedGateway
from .models import BetaCampaignStatus
from .service import UnsafeOperation, ValidationFailed
from .vault import CredentialMaterial


class CampaignWorkerRuntimeMixin:
    def _run(self, record: CampaignRecord, material: CredentialMaterial, stop: threading.Event) -> None:
        campaign_id = record.campaign_id
        profile: LiveProfile | None = None
        gateway: BoundedGateway | None = None
        snapshot_gateway: BoundedGateway | None = None
        lanes: dict[str, BoundedGateway] = {}
        websocket_runtime: WeexCampaignWebSocketRuntime | None = None
        phase_keys: dict[tuple[int, str], str] = {}
        emergency_io = threading.Event()

        def event_sink(payload: dict[str, Any]) -> None:
            name = str(payload.get("event") or payload.get("name") or "")
            if name.startswith("safe_stop"):
                emergency_io.set()
            round_number = payload.get("round")
            if isinstance(round_number, int):
                if name == "hold_started":
                    self.phase_pacer.finish(phase_keys.get((round_number, "open"), ""))
                elif name in {"cycle_completed", "cycle_stopped"}:
                    self.phase_pacer.finish(phase_keys.get((round_number, "close"), ""))
            try:
                event = _sanitize_event(payload)
                publish_monitor_event(self, record, event)
            except Exception:
                return

        def phase_waiter(plan_id: str, phase: str, round_number: int) -> bool:
            key = f"{record.campaign_id}:{plan_id}:{round_number}:{phase}"
            phase_keys[(round_number, phase)] = key
            return self.phase_pacer.wait(
                key,
                phase=phase,
                round_number=round_number,
                proxy_key=(profile.proxy_url if profile is not None and profile.proxy_url else "direct"),
                stop_event=stop,
                event_sink=event_sink,
            )

        try:
            profile, raw_gateway = self._profile_and_gateway(material)
            gateway = BoundedGateway(raw_gateway, self.io_budget, emergency_io)
            provider = self.beta_provider_factory()
            snapshot_gateway = gateway.fork()
            lanes = {"BTC": gateway.fork(), "ETH": gateway.fork()}
            # Resolve these two runtime collaborators through the legacy facade.
            # Existing integrations patch that public seam, while the worker stays
            # isolated from the facade for every other dependency.
            from . import campaigns as campaign_api

            if self.settings.live_campaign_websockets_enabled:
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
                phase_waiter=phase_waiter,
            ).execute(record.campaign)
            try:
                ending_available_balance = _available_quote(gateway)
            except Exception:  # A missing audit snapshot must not change the execution outcome.
                ending_available_balance = None
            status = str(result.get("status") or BetaCampaignStatus.UNCERTAIN.value)
            if status not in {item.value for item in BetaCampaignStatus}:
                status = BetaCampaignStatus.UNCERTAIN.value
            latest = self.journal.get(campaign_id) or record
            if status == BetaCampaignStatus.UNCERTAIN.value:
                status = (
                    BetaCampaignStatus.RECOVERING.value
                    if submission_attempted(latest)
                    else BetaCampaignStatus.STOPPED.value
                )
                result = {
                    **result,
                    "status": status,
                    "reason": (
                        str(result.get("reason") or "execution_outcome_uncertain")
                        if status == BetaCampaignStatus.RECOVERING.value
                        else f"launch_aborted:{result.get('reason') or 'pre_submission_failure'}"
                    ),
                }
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
        except Exception as exc:  # noqa: BLE001 - classification depends on the durable submission boundary
            reason = _worker_exception_reason(exc)
            latest = self.journal.get(campaign_id) or record
            attempted = submission_attempted(latest)
            status = BetaCampaignStatus.RECOVERING.value if attempted else BetaCampaignStatus.STOPPED.value
            stored_reason = reason if attempted else f"launch_aborted:{reason}"
            self.journal.update(
                campaign_id,
                status=status,
                finished_at_ms=int(time.time() * 1000),
                reason=stored_reason,
            )
            event = _sanitize_event(
                {
                    "event": "campaign_recovering" if attempted else "launch_aborted",
                    "error": type(exc).__name__,
                    "reason": stored_reason,
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
            self.phase_pacer.release_execution(campaign_id)
            if lease is not None:
                lease.release()
            self._notify(record.instance_id)

    def _profile_and_gateway(self, material: CredentialMaterial) -> tuple[LiveProfile, WeexGateway]:
        return build_live_profile_gateway(self.settings, material)

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
        return self.write_coordinator.critical(lambda: append_monitor_event_direct(self, record, event))

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

    def verify_bound_strategy_recovery(
        self,
        record: CampaignRecord | None,
        material: CredentialMaterial,
    ) -> Decimal:
        """Read and verify the recovery boundary without issuing a mutation."""
        gateway: WeexGateway | None = None
        try:
            profile, gateway = self._profile_and_gateway(material)
            if record is not None and live_profile_fingerprint(profile) != record.campaign.profile_fingerprint:
                raise UnsafeOperation("旧任务无法自动收尾：Live 配置已发生变化")
            boundary = inspect_live_account(gateway, Decimal(0))
            if not _account_boundary_is_flat(boundary):
                positions = int(boundary.get("active_position_count") or 0)
                orders = int(boundary.get("regular_order_count") or 0)
                triggers = int(boundary.get("trigger_order_count") or 0)
                raise UnsafeOperation(
                    f"旧任务尚不能收尾：仍有 {positions} 个持仓、{orders} 个挂单、{triggers} 个条件单"
                )
            return _available_quote_from_readiness(boundary)
        finally:
            if gateway is not None:
                gateway.close()

    def inspect_bound_strategy_boundary(
        self,
        material: CredentialMaterial,
    ) -> dict[str, object]:
        """Read the current account boundary without applying any lifecycle transition."""
        gateway: WeexGateway | None = None
        try:
            _, gateway = self._profile_and_gateway(material)
            boundary = inspect_live_account(gateway, Decimal(0))
            return {
                "flat": _account_boundary_is_flat(boundary),
                "position_count": int(boundary.get("active_position_count") or 0),
                "regular_order_count": int(boundary.get("regular_order_count") or 0),
                "trigger_order_count": int(boundary.get("trigger_order_count") or 0),
                "available_quote": str(_available_quote_from_readiness(boundary)),
            }
        finally:
            if gateway is not None:
                gateway.close()

    def archive_bound_strategy_recovery(self, record: CampaignRecord, *, recovered_at_ms: int) -> None:
        """Make a verified historical Campaign non-blocking and retain its audit trail."""
        if record.status in {BetaCampaignStatus.EXECUTING.value, BetaCampaignStatus.STOPPING.value}:
            raise UnsafeOperation("旧任务仍在执行或停止中，不能创建新任务")
        updates: dict[str, Any] = {
            "reconciliation_acknowledged_at_ms": recovered_at_ms,
            "reconciliation_boundary": "btc_eth_flat_no_regular_or_trigger_orders",
            "reconciliation_source": "automatic_startup_recovery",
        }
        if record.status in {
            BetaCampaignStatus.UNCERTAIN.value,
            BetaCampaignStatus.RECOVERING.value,
            BetaCampaignStatus.PLANNED.value,
        }:
            updates.update(
                status=BetaCampaignStatus.STOPPED.value,
                finished_at_ms=recovered_at_ms,
                reason="automatic_startup_recovery",
            )
        self.journal.update(record.campaign_id, **updates)
        event = _sanitize_event({"event": "campaign_recovery_archived", "verified": True})
        event["sequence"] = self._append_monitor_event(record, event)
        self._notify(record.instance_id)

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

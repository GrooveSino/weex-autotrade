"""Single-command residual order cancellation and Maker-only flattening."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from weex_cli.beta_allocation import BetaAllocation
from weex_cli.beta_campaign import (
    BetaVolumeCampaign,
    BetaVolumeCampaignStore,
    inspect_live_account,
    live_profile_fingerprint,
)
from weex_cli.beta_volume import BetaVolumePlan, BetaVolumePlanStore
from weex_cli.gateway import WeexGateway

from .campaign_contracts import _AccountLease
from .campaign_events import _sanitize_event
from .campaign_helpers import (
    _account_boundary_is_flat,
    _bound_strategy_confirmation,
    _bound_strategy_stop_confirmation,
    _cleanup_confirmation,
    _preview_metadata,
)
from .models import AccountInstance, BetaCampaignStatus
from .service import UnsafeOperation
from .vault import CredentialMaterial


class CampaignCleanupMixin:
    def prepare_bound_strategy_cleanup(
        self,
        instance: AccountInstance,
        material: CredentialMaterial,
        boundary: dict[str, object],
    ):
        """Persist a cleanup-only Campaign without requiring the Beta feed."""
        self._require_live_gate()
        profile, gateway = self._profile_and_gateway(material)
        try:
            now_ms = int(time.time() * 1000)
            allocation = BetaAllocation(
                beta=Decimal(1),
                btc_long_weight=Decimal("0.5"),
                eth_short_weight=Decimal("0.5"),
                version=f"cleanup-boundary:{now_ms}",
                as_of_ms=now_ms,
                confidence=Decimal(1),
                confidence_threshold=Decimal(0),
                source="cleanup_boundary",
            )
            cleanup_target = max(instance.strategy.round_turnover_quote_max, Decimal("1000"))
            campaign = BetaVolumeCampaign.create(
                gateway,
                allocation,
                profile_fingerprint=live_profile_fingerprint(profile),
                target_turnover_quote=cleanup_target,
                round_turnover_quote=cleanup_target,
                hold_min_seconds=0,
                hold_max_seconds=0,
                round_gap_min_seconds=0,
                round_gap_max_seconds=0,
                now_ms=now_ms,
            )
            readiness = {
                "available_quote": boundary["available_quote"],
                "active_position_count": boundary["position_count"],
                "regular_order_count": boundary["regular_order_count"],
                "trigger_order_count": boundary["trigger_order_count"],
            }
            metadata = _preview_metadata(campaign, Decimal(str(boundary["available_quote"])), readiness)
            metadata.update(
                {
                    "execution_kind": "bound_strategy",
                    "confirmation": _bound_strategy_confirmation(campaign),
                    "stop_confirmation": _bound_strategy_stop_confirmation(campaign.campaign_id),
                    "strategy_id": instance.strategy.id,
                    "strategy_name": instance.strategy.name,
                    "strategy_version": instance.strategy.version,
                    "strategy_snapshot": instance.strategy.model_dump(mode="json", by_alias=True),
                    "owner_user_id": instance.owner_user_id,
                    "reason": "cleanup_required",
                    "cleanup_required": True,
                    **self._cleanup_counts(boundary),
                }
            )
            BetaVolumeCampaignStore(self.settings.campaign_data_directory / instance.id).create(campaign)
            self.journal.create(instance.id, campaign, metadata)
            self.journal.update(campaign.campaign_id, status=BetaCampaignStatus.RECOVERING.value)
            return self.journal.get(campaign.campaign_id)
        finally:
            gateway.close()

    def cleanup_bound_strategy(
        self,
        instance_id: str,
        campaign_id: str,
        confirmation: str,
        material: CredentialMaterial | None,
    ) -> dict[str, object]:
        """Cancel once, Maker-flatten once, then prove the account boundary."""
        self._require_live_gate()
        record = self._require_record(instance_id, campaign_id)
        if record.status not in {BetaCampaignStatus.RECOVERING.value, BetaCampaignStatus.UNCERTAIN.value}:
            raise UnsafeOperation("strategy cleanup is only available while recovery is active")
        if confirmation != _cleanup_confirmation(campaign_id):
            raise UnsafeOperation("exact cleanup confirmation does not match")
        if material is None:
            raise UnsafeOperation("account credentials are unavailable")
        with self._lock:
            if instance_id in self._cleaning:
                raise UnsafeOperation("strategy cleanup is already running for this account")
            self._cleaning.add(instance_id)
        store = BetaVolumePlanStore(self.settings.campaign_data_directory / instance_id / "plans")
        gateway: WeexGateway | None = None
        lanes: dict[str, WeexGateway] = {}
        lease = _AccountLease(
            self.settings.campaign_data_directory,
            material.api_key.get_secret_value(),
            instance_id,
            campaign_id,
        )
        try:
            lease.acquire()
            profile, gateway = self._profile_and_gateway(material)
            plan = self._create_cleanup_plan(record.campaign, gateway)
            store.create(plan)
            store.save(plan, state="stopped")
            self._append_monitor_event(
                record,
                _sanitize_event(
                    {
                        "event": "cleanup_plan_created",
                        "child_plan_id": plan.plan_id,
                        "reason": "explicit_user_cleanup",
                    }
                ),
            )
            store.claim_for_recovery(plan)

            def event_sink(payload: dict[str, Any]) -> None:
                event = _sanitize_event(payload)
                event["sequence"] = self._append_monitor_event(record, event)
                self._notify_progress(instance_id, event)

            assert gateway is not None
            lanes = {"BTC": gateway.fork(), "ETH": gateway.fork()}
            from . import campaigns as campaign_api

            result = campaign_api.LiveBetaVolumeCampaignService(
                gateway,
                self.beta_provider_factory(),
                BetaVolumeCampaignStore(self.settings.campaign_data_directory / instance_id),
                store,
                profile_fingerprint=live_profile_fingerprint(profile),
                event_sink=event_sink,
                lane_gateways=lanes,
            ).cleanup(plan)
            boundary = inspect_live_account(gateway, Decimal(0))
            verified = _account_boundary_is_flat(boundary) and result.get("status") == "stopped"
            now_ms = int(time.time() * 1000)
            self.journal.update(
                campaign_id,
                status=(BetaCampaignStatus.STOPPED.value if verified else BetaCampaignStatus.RECOVERING.value),
                finished_at_ms=now_ms if verified else None,
                reason="cleanup_completed" if verified else str(result.get("reason") or "cleanup_unverified"),
                cleanup_required=not verified,
                cleanup_completed_at_ms=now_ms if verified else None,
            )
            return {
                "verified": verified,
                "reason": str(result.get("reason") or "cleanup_unverified"),
                "position_count": int(boundary.get("active_position_count") or 0),
                "regular_order_count": int(boundary.get("regular_order_count") or 0),
                "trigger_order_count": int(boundary.get("trigger_order_count") or 0),
            }
        finally:
            for lane in lanes.values():
                lane.close()
            if gateway is not None:
                gateway.close()
            lease.release()
            with self._lock:
                self._cleaning.discard(instance_id)

    @staticmethod
    def _cleanup_counts(boundary: dict[str, object]) -> dict[str, int]:
        return {
            "position_count": int(boundary.get("position_count") or 0),
            "regular_order_count": int(boundary.get("regular_order_count") or 0),
            "trigger_order_count": int(boundary.get("trigger_order_count") or 0),
        }

    @staticmethod
    def _create_cleanup_plan(campaign: BetaVolumeCampaign, gateway: WeexGateway) -> BetaVolumePlan:
        return BetaVolumePlan.create(
            gateway,
            campaign.allocation,
            target_turnover_quote=campaign.round_turnover_quote,
            round_turnover_quote=campaign.round_turnover_quote,
            max_position_quote=campaign.max_position_quote,
            timeout_seconds=campaign.timeout_seconds,
            recovery_attempts=campaign.recovery_attempts,
            max_empty_rounds=campaign.max_empty_rounds,
            cooldown_seconds=campaign.cooldown_seconds,
            leverage=campaign.leverage,
            max_auto_leverage=campaign.max_auto_leverage,
            margin_buffer=campaign.margin_buffer,
            margin_mode=campaign.margin_mode,
            direction=campaign.direction,
            now_ms=int(time.time() * 1000),
        )

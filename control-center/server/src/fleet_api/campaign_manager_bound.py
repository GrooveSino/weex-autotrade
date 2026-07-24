from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal
from typing import Any

from weex_cli.beta_allocation import BetaUnavailable
from weex_cli.beta_campaign import (
    BetaVolumeCampaign,
    BetaVolumeCampaignStore,
    inspect_live_account,
    live_profile_fingerprint,
)

from .campaign_contracts import CampaignRecord
from .campaign_events import _sanitize_event, _view
from .campaign_helpers import (
    _available_quote_from_readiness,
    _bound_strategy_confirmation,
    _bound_strategy_stop_confirmation,
    _preview_metadata,
)
from .models import BetaCampaignPreview, BetaCampaignStatus, StrategyDirection, VolumeStrategy
from .ownership import LEGACY_OWNER_USER_ID
from .service import BetaSourceUnavailable, UnsafeOperation
from .vault import CredentialMaterial

FIXED_BOUND_STRATEGY_LEVERAGE = 400


class CampaignBoundStrategyMixin:
    def preview_bound_strategy(
        self,
        instance_id: str,
        strategy: VolumeStrategy,
        target_quote: Decimal,
        material: CredentialMaterial | None,
        *,
        session_id: str | None,
        target_mode: str = "incremental",
        run_disposition: str = "new_incremental",
        strategy_target_quote: Decimal | None = None,
        baseline_lifetime_quote: Decimal = Decimal(0),
        direction: StrategyDirection = StrategyDirection.BTC_LONG_ETH_SHORT,
        owner_user_id: str = LEGACY_OWNER_USER_ID,
    ) -> BetaCampaignPreview:
        """Create an executable Live preview solely from a persisted strategy binding."""
        self._require_live_gate()
        if target_quote <= 0:
            raise UnsafeOperation("bound strategy has no remaining verified target")
        if material is None:
            raise UnsafeOperation("account credentials are unavailable")
        invalidated = self._invalidate_stale_preview_for_current_strategy(instance_id, strategy, direction)
        active = self.journal.active_for_instance(instance_id)
        if active is not None:
            if active.metadata.get("strategy_id") != strategy.id:
                raise UnsafeOperation("this account already has an active execution for another bound strategy")
            return _view(active, include_events=False)  # type: ignore[return-value]
        if invalidated:
            self._notify(instance_id)
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
                target_turnover_quote=target_quote,
                round_turnover_quote=strategy.round_turnover_quote_max,
                round_turnover_quote_min=strategy.round_turnover_quote_min,
                hold_min_seconds=strategy.position_hold_min_seconds,
                hold_max_seconds=strategy.position_hold_max_seconds,
                round_gap_min_seconds=strategy.round_interval_min_seconds,
                round_gap_max_seconds=strategy.round_interval_max_seconds,
                leverage=FIXED_BOUND_STRATEGY_LEVERAGE,
                margin_mode="cross",
                direction=direction.value,
            )
            opening_notional = min(campaign.round_turnover_quote, campaign.target_turnover_quote) / Decimal(2)
            required = opening_notional / Decimal(FIXED_BOUND_STRATEGY_LEVERAGE) * campaign.margin_buffer
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
            if (
                readiness.get("active_position_count", 0)
                or readiness.get("regular_order_count", 0)
                or readiness.get("trigger_order_count", 0)
            ):
                blockers.append("account_is_not_flat")
            if blockers:
                raise UnsafeOperation(f"bound strategy preview blocked: {','.join(blockers)}")
            metadata = _preview_metadata(campaign, available, readiness)
            metadata.update(
                {
                    "execution_kind": "bound_strategy",
                    "confirmation": _bound_strategy_confirmation(campaign),
                    "stop_confirmation": _bound_strategy_stop_confirmation(campaign.campaign_id),
                    "strategy_id": strategy.id,
                    "strategy_name": strategy.name,
                    "strategy_version": strategy.version,
                    "strategy_snapshot": strategy.model_dump(mode="json", by_alias=True),
                    "session_id": session_id,
                    "session_target_quote": str(target_quote),
                    "target_mode": target_mode,
                    "run_disposition": run_disposition,
                    "strategy_target_quote": str(strategy_target_quote or target_quote),
                    "baseline_lifetime_quote": str(baseline_lifetime_quote),
                    "direction": direction.value,
                    "owner_user_id": owner_user_id,
                }
            )
            self.journal.create(instance_id, campaign, metadata)
            BetaVolumeCampaignStore(self.settings.campaign_data_directory / instance_id).create(campaign)
            return _view(self.journal.get(campaign.campaign_id), include_events=False)  # type: ignore[arg-type]
        finally:
            gateway.close()

    def apply_bound_strategy_change(
        self,
        instance_ids: Iterable[str],
        apply: Callable[[], Any],
        *,
        reason: str,
    ) -> Any:
        """Atomically persist a binding change and retire only its unexecuted previews.

        Planned bound-strategy previews are immutable authorization artifacts.  They
        cannot be edited to match a new shared strategy, because that would let an
        old exact confirmation authorize a different execution.  They are safe to
        retire: no worker has claimed them and no exchange operation has occurred.
        """
        affected = tuple(dict.fromkeys(instance_ids))
        with self._lock:
            self._assert_bound_strategy_change_allowed(affected)
            result = apply()
            invalidated = self._invalidate_planned_bound_strategy_previews_locked(affected, reason=reason)
        for instance_id in invalidated:
            self._notify(instance_id)
        return result

    def invalidate_stale_planned_bound_strategy_previews(
        self,
        strategies_by_instance: Mapping[str, VolumeStrategy],
        *,
        reason: str,
    ) -> list[str]:
        """Retire persisted previews whose immutable strategy snapshot is stale.

        This is used during executor startup to repair planned previews created by
        older releases, before they can shadow an account's current binding.
        """
        with self._lock:
            invalidated: list[str] = []
            for instance_id, strategy in strategies_by_instance.items():
                record = self.journal.active_for_instance(instance_id)
                if record is None or not self._is_stale_bound_strategy_preview(record, strategy):
                    continue
                self._invalidate_planned_record_locked(record, reason=reason)
                invalidated.append(instance_id)
        for instance_id in invalidated:
            self._notify(instance_id)
        return invalidated

    def _invalidate_stale_preview_for_current_strategy(
        self,
        instance_id: str,
        strategy: VolumeStrategy,
        direction: StrategyDirection | None = None,
    ) -> bool:
        with self._lock:
            record = self.journal.active_for_instance(instance_id)
            if record is None or not self._is_stale_bound_strategy_preview(record, strategy, direction):
                return False
            self._invalidate_planned_record_locked(record, reason="bound_strategy_version_stale")
            return True

    def _assert_bound_strategy_change_allowed(self, instance_ids: Iterable[str]) -> None:
        for instance_id in instance_ids:
            for record in self.journal.list_for_instance(instance_id):
                if record.metadata.get("execution_kind") != "bound_strategy":
                    continue
                if record.status in {BetaCampaignStatus.EXECUTING.value, BetaCampaignStatus.STOPPING.value}:
                    raise UnsafeOperation(
                        "cannot change a bound strategy while its Live execution is active; stop and verify it first"
                    )
                if record.status in {
                    BetaCampaignStatus.RECOVERING.value,
                    BetaCampaignStatus.UNCERTAIN.value,
                }:
                    raise UnsafeOperation("cannot change a bound strategy while its Live execution recovery is active")

    def _invalidate_planned_bound_strategy_previews_locked(
        self, instance_ids: Iterable[str], *, reason: str
    ) -> list[str]:
        invalidated: list[str] = []
        for instance_id in instance_ids:
            for record in self.journal.list_for_instance(instance_id):
                if not self._is_planned_bound_strategy_preview(record):
                    continue
                self._invalidate_planned_record_locked(record, reason=reason)
                invalidated.append(instance_id)
        return invalidated

    @staticmethod
    def _is_planned_bound_strategy_preview(record: CampaignRecord) -> bool:
        return (
            record.status == BetaCampaignStatus.PLANNED.value
            and record.metadata.get("execution_kind") == "bound_strategy"
        )

    @classmethod
    def _is_stale_bound_strategy_preview(
        cls,
        record: CampaignRecord,
        strategy: VolumeStrategy,
        direction: StrategyDirection | None = None,
    ) -> bool:
        if not cls._is_planned_bound_strategy_preview(record):
            return False
        return (
            record.metadata.get("strategy_id") != strategy.id
            or record.metadata.get("strategy_version") != strategy.version
            or record.campaign.schema_version < 4
            or record.campaign.leverage != FIXED_BOUND_STRATEGY_LEVERAGE
            or record.campaign.margin_mode != "cross"
            or (direction is not None and record.campaign.direction != direction.value)
        )

    def _invalidate_planned_record_locked(self, record: CampaignRecord, *, reason: str) -> None:
        invalidated_at_ms = int(time.time() * 1000)
        self.journal.update(
            record.campaign_id,
            status=BetaCampaignStatus.STOPPED.value,
            reason=reason,
            invalidated_at_ms=invalidated_at_ms,
            invalidation_reason=reason,
        )
        self._append_monitor_event(
            record,
            _sanitize_event(
                {
                    "event": "bound_strategy_preview_invalidated",
                    "reason": reason,
                    "strategy_id": record.metadata.get("strategy_id"),
                    "strategy_version": record.metadata.get("strategy_version"),
                },
            ),
        )

"""Single public command surface for one account's strategy-run lifecycle."""

from __future__ import annotations

from typing import Any

from .campaign_events import _view
from .campaign_helpers import _cleanup_confirmation
from .models import AccountInstance, BetaCampaignView, TradingMode
from .service import UnsafeOperation
from .strategy_run_types import LifecyclePreparation
from .vault import CredentialMaterial


class StrategyRunCommandMixin:
    """Route all user-visible execution transitions through the lifecycle owner."""

    def prepare_planned(
        self,
        instance: AccountInstance,
        material: CredentialMaterial,
        record: Any,
    ) -> LifecyclePreparation:
        try:
            boundary = self._manager.inspect_bound_strategy_boundary(material)
        except Exception as exc:  # read-only and retryable
            return LifecyclePreparation(
                "unavailable",
                reason_code=f"boundary_unavailable:{type(exc).__name__.lower()}",
                message="账户持仓与挂单边界暂时不可用，请重试",
            )
        if bool(boundary["flat"]):
            return LifecyclePreparation("ready", execution=_view(record, include_events=False))
        self._journal.update(
            record.campaign_id,
            status="stopped",
            finished_at_ms=self._now_ms(),
            reason="launch_preview_boundary_changed",
        )
        cleanup_record = self._manager.prepare_bound_strategy_cleanup(instance, material, boundary)
        counts = self._boundary_counts(boundary)
        return LifecyclePreparation(
            "cleanup_required",
            execution=self._view_or_none(cleanup_record),
            cleanup_confirmation=_cleanup_confirmation(cleanup_record.campaign_id),
            **counts,
        )

    def start_run(
        self,
        instance: AccountInstance,
        execution_id: str,
        confirmation: str,
        risk_acknowledged: bool,
        material: CredentialMaterial | None,
    ) -> BetaCampaignView:
        if instance.mode is not TradingMode.LIVE:
            raise UnsafeOperation("bound strategy execution requires a Live account")
        preview = self._manager.get(instance.id, execution_id)
        if preview.strategy_id is None:
            raise UnsafeOperation("execution was not created from this account's bound strategy")
        if preview.strategy_id != instance.strategy_id or preview.strategy_version != instance.strategy.version:
            raise UnsafeOperation("bound strategy changed since preview; create a new preview and confirm again")
        return self._manager.start(instance.id, execution_id, confirmation, risk_acknowledged, material)

    def create_run_preview(
        self,
        instance: AccountInstance,
        plan: Any,
        material: CredentialMaterial | None,
        *,
        session_id: str,
    ) -> BetaCampaignView:
        return self._manager.preview_bound_strategy(
            instance.id,
            instance.strategy,
            plan.execution_target_quote_volume,
            material,
            session_id=session_id,
            target_mode=plan.target_mode.value,
            run_disposition=plan.run_disposition,
            strategy_target_quote=plan.strategy_target_quote_volume,
            baseline_lifetime_quote=plan.baseline_lifetime_quote_volume,
            owner_user_id=instance.owner_user_id,
        )

    def stop_run(self, instance: AccountInstance, execution_id: str, confirmation: str) -> BetaCampaignView:
        preview = self._manager.get(instance.id, execution_id)
        if preview.strategy_id is None:
            raise UnsafeOperation("execution was not created from this account's bound strategy")
        return self._manager.stop(instance.id, execution_id, confirmation)

    def cleanup_run(
        self,
        instance: AccountInstance,
        confirmation: str,
        material: CredentialMaterial | None,
    ) -> dict[str, object]:
        lifecycle = self.projection(instance.id, instance.mode.value)
        if lifecycle.state != "cleanup_required" or lifecycle.execution_id is None:
            raise UnsafeOperation("this account does not currently require strategy cleanup")
        return self._manager.cleanup_bound_strategy(
            instance.id,
            lifecycle.execution_id,
            confirmation,
            material,
        )

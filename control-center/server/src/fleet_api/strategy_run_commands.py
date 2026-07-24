"""Single public command surface for one account's strategy-run lifecycle."""

from __future__ import annotations

from typing import Any

from .campaign_events import _view
from .models import AccountInstance, BetaCampaignView, TradingMode
from .service import UnsafeOperation
from .strategy_run_types import LifecyclePreparation
from .vault import CredentialMaterial


class StrategyRunCommandMixin:
    """Route all user-visible execution transitions through the lifecycle owner."""

    def prepare_planned(
        self,
        _instance: AccountInstance,
        _material: CredentialMaterial,
        record: Any,
    ) -> LifecyclePreparation:
        # Reopening an immutable preview is local-only. start_run performs the
        # authoritative flat/no-orders check again before worker admission.
        return LifecyclePreparation("ready", execution=_view(record, include_events=False))

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
        boundary: dict[str, object] | None = None,
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
            direction=plan.direction,
            owner_user_id=instance.owner_user_id,
            boundary_snapshot=boundary,
        )

    def stop_run(
        self,
        instance: AccountInstance,
        execution_id: str,
        confirmation: str,
        material: CredentialMaterial | None = None,
    ) -> BetaCampaignView:
        preview = self._manager.get(instance.id, execution_id)
        if preview.strategy_id is None:
            raise UnsafeOperation("execution was not created from this account's bound strategy")
        return self._manager.stop(instance.id, execution_id, confirmation, material)

    def cleanup_run(
        self,
        instance: AccountInstance,
        confirmation: str,
        material: CredentialMaterial | None,
    ) -> dict[str, object]:
        boundary = self._journal.boundary_projection(instance.id) or {}
        if not int(boundary.get("regular_order_count") or 0) and not int(boundary.get("trigger_order_count") or 0):
            raise UnsafeOperation("该账号当前没有需要撤销的启动前挂单")
        return self._manager.cleanup_bound_strategy(
            instance.id,
            confirmation,
            material,
        )

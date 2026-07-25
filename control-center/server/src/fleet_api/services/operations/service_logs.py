from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from uuid import uuid4

from fleet_api.auth.ownership import LEGACY_OWNER_USER_ID, current_owner_user_id
from fleet_api.campaigns.core.campaign_log import campaign_event_log
from fleet_api.market.funding import funding_preflight
from fleet_api.models import (
    AccountInstance,
    InstanceStatus,
    LogBatch,
    LogLevel,
    LogLine,
    StrategyProgress,
    StrategyStage,
    VolumeStrategy,
    default_volume_strategy,
)
from fleet_api.services.control.service_errors import (
    UnsafeOperation,
)
from fleet_api.services.control.service_shared import now as _now
from fleet_api.strategy.strategy import estimate_rounds, target_progress_quote


class ServiceLogsMixin:
    def logs(self, instance_id: str, limit: int) -> list[LogLine]:
        instance = self.get_instance(instance_id)
        lines = self.repository.read_logs(instance_id, limit)
        if instance.unread_logs:
            self.repository.replace(instance.model_copy(update={"unread_logs": 0}, deep=True))
        return lines

    def record_refresh_success(self, instance_id: str) -> AccountInstance:
        """Record an explicit user-triggered refresh without logging background polls."""
        instance = self.get_instance(instance_id)
        updated = instance.model_copy(
            update={"unread_logs": instance.unread_logs + 1},
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(instance_id, LogLevel.SUCCESS, "刷新成功：价格、钱包与仓位已同步")
        return updated

    def record_campaign_progress(self, instance_id: str, event: Mapping[str, object]) -> None:
        """Project a safe, durable Campaign event into the account log stream.

        The worker has already persisted the event before calling here.  This is
        strictly an observability projection and cannot alter execution state.
        """
        self.get_instance(instance_id)
        rendered = campaign_event_log(event)
        if rendered is None:
            return
        level, message = rendered
        self._append_log(instance_id, level, message)

    def clear_logs(self, instance_id: str, execution_boundaries: Mapping[str, int] | None = None) -> None:
        instance = self.get_instance(instance_id)
        self.repository.clear_logs(instance_id, execution_boundaries)
        if instance.unread_logs:
            self.repository.replace(instance.model_copy(update={"unread_logs": 0}, deep=True))

    def log_updates(self, instance_id: str, limit: int, after: str | None) -> LogBatch:
        instance = self.get_instance(instance_id)
        window = self.repository.read_logs(instance_id, 500)
        reset = False
        if after is None:
            lines = window[-limit:]
        else:
            cursor_index = next((index for index, line in enumerate(window) if line.id == after), None)
            if cursor_index is None:
                lines = window[-limit:]
                reset = True
            else:
                lines = window[cursor_index + 1 : cursor_index + 1 + limit]
        if instance.unread_logs:
            self.repository.replace(instance.model_copy(update={"unread_logs": 0}, deep=True))
        cursor = lines[-1].id if lines else (None if reset else after)
        return LogBatch(lines=lines, cursor=cursor, reset=reset)

    @staticmethod
    def validate_global_stop_confirmation(confirmation: str) -> None:
        if confirmation != "STOP ALL":
            raise UnsafeOperation("confirmation mismatch; expected exactly: STOP ALL")

    def delete_instance(self, instance_id: str) -> None:
        instance = self.get_instance(instance_id)
        if instance.status is not InstanceStatus.STOPPED:
            raise UnsafeOperation("stop the instance before deleting it")
        if instance.strategy_progress.stage is StrategyStage.HOLDING:
            raise UnsafeOperation("resume and close the active mock pair before deleting the instance")
        self.repository.delete(instance_id)
        self.vault.remove(instance_id)

    def _append_log(self, instance_id: str, level: LogLevel, message: str) -> None:
        self.repository.append_log(
            instance_id,
            LogLine(id=uuid4().hex, timestamp=_now(), level=level, message=message),
        )

    def _reconcile_strategy_catalog(self) -> None:
        strategies = {strategy.id: strategy for strategy in self.repository.list_strategies()}
        projections: list[AccountInstance] = []
        for instance in self.repository.list():
            canonical = strategies.get(instance.strategy.id)
            if canonical is None:
                canonical = self.repository.create_strategy(instance.strategy)
                strategies[canonical.id] = canonical
            projected = instance.model_copy(
                update={"strategy_id": canonical.id, "strategy": canonical},
                deep=True,
            )
            projected = self._with_funding(projected)
            if projected != instance:
                projections.append(projected)
        if projections:
            self.repository.replace_many(projections)
        if not strategies:
            self.repository.create_strategy(default_volume_strategy())

    def _default_strategy(self) -> VolumeStrategy:
        owner = current_owner_user_id() or LEGACY_OWNER_USER_ID
        strategy_id = "strategy-default" if owner == LEGACY_OWNER_USER_ID else f"strategy-default-{owner}"
        strategy = self.repository.get_strategy(strategy_id)
        if strategy is not None:
            return strategy
        strategies = self.repository.list_strategies()
        if strategies:
            return strategies[0]
        return self.repository.create_strategy(
            default_volume_strategy().model_copy(update={"id": strategy_id, "owner_user_id": owner})
        )

    def _create_legacy_strategy(self, *, cycle_target: int, legacy_total_quote: Decimal) -> VolumeStrategy:
        round_turnover = legacy_total_quote * Decimal(2)
        strategy = VolumeStrategy(
            id=f"strategy-{uuid4().hex[:10]}",
            owner_user_id=current_owner_user_id() or LEGACY_OWNER_USER_ID,
            name="兼容固定轮次策略",
            target_volume_quote=round_turnover * cycle_target,
            round_turnover_quote_min=round_turnover,
            round_turnover_quote_max=round_turnover,
            position_hold_min_seconds=0,
            position_hold_max_seconds=0,
            round_interval_min_seconds=0,
            round_interval_max_seconds=0,
        )
        return self.repository.create_strategy(strategy)

    @staticmethod
    def _ensure_configurable(instance: AccountInstance, operation: str) -> None:
        if instance.status is not InstanceStatus.STOPPED:
            raise UnsafeOperation(f"stop instance {instance.id!r} before {operation}")
        has_open_pair = (
            instance.strategy_progress.stage is StrategyStage.HOLDING
            or instance.exposure.btc_long != 0
            or instance.exposure.eth_short != 0
        )
        if has_open_pair:
            raise UnsafeOperation(f"close the active pair on instance {instance.id!r} before {operation}")

    def _strategy_projection(
        self,
        instance: AccountInstance,
        strategy: VolumeStrategy,
        *,
        reset_progress: bool,
    ) -> AccountInstance:
        if reset_progress:
            progress = StrategyProgress()
        else:
            projected = instance.model_copy(update={"strategy": strategy}, deep=True)
            achieved = target_progress_quote(projected, strategy)
            progress = instance.strategy_progress.model_copy(
                update={
                    "stage": (
                        StrategyStage.COMPLETE if achieved >= strategy.target_volume_quote else StrategyStage.IDLE
                    ),
                    "next_action_at_ms": None,
                    "active_cycle_id": None,
                }
            )
        projected = instance.model_copy(
            update={"strategy_id": strategy.id, "strategy": strategy, "strategy_progress": progress},
            deep=True,
        )
        achieved = target_progress_quote(projected)
        remaining_rounds = estimate_rounds(strategy, achieved).maximum
        cycle = instance.cycle.model_copy(
            update={
                "target": max(1, instance.cycle.completed + remaining_rounds),
                "next_action_at": None,
            }
        )
        complete = achieved >= strategy.target_volume_quote
        updated = instance.model_copy(
            update={
                "strategy_id": strategy.id,
                "strategy": strategy,
                "strategy_progress": progress,
                "cycle": cycle,
                "phase": "目标交易量已完成" if complete else "策略已更新，等待启动",
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        return self._with_funding(updated)

    @staticmethod
    def _with_funding(instance: AccountInstance, *, wallet_known: bool | None = None) -> AccountInstance:
        known = (
            wallet_known
            if wallet_known is not None
            else instance.runtime.last_poll_succeeded_at_ms is not None
            or instance.wallet.equity > 0
            or instance.wallet.available > 0
        )
        preflight = funding_preflight(
            instance.strategy,
            instance.wallet.available,
            wallet_known=known,
        )
        return instance.model_copy(update={"funding_preflight": preflight}, deep=True)

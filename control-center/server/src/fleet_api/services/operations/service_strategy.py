from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from fleet_api.auth.ownership import LEGACY_OWNER_USER_ID, current_owner_user_id
from fleet_api.models import (
    AccountInstance,
    LogLevel,
    VolumeStrategy,
    VolumeStrategyInput,
)
from fleet_api.services.control.service_errors import (
    StrategyNotFound,
    UnsafeOperation,
    ValidationFailed,
)
from fleet_api.strategy.strategy import target_progress_quote


class ServiceStrategyMixin:
    def list_strategies(self) -> list[VolumeStrategy]:
        return self.repository.list_strategies()

    def get_strategy(self, strategy_id: str) -> VolumeStrategy:
        strategy = self.repository.get_strategy(strategy_id)
        if strategy is None:
            raise StrategyNotFound(f"strategy {strategy_id!r} was not found")
        return strategy

    def create_strategy(self, request: VolumeStrategyInput) -> VolumeStrategy:
        strategy = VolumeStrategy(
            id=f"strategy-{uuid4().hex[:10]}",
            owner_user_id=current_owner_user_id() or LEGACY_OWNER_USER_ID,
            version=1,
            **request.model_dump(),
        )
        return self.repository.create_strategy(strategy)

    def duplicate_strategy(self, strategy_id: str) -> VolumeStrategy:
        source = self.get_strategy(strategy_id)
        names = {strategy.name.casefold() for strategy in self.list_strategies()}
        duplicate = source.model_copy(
            update={
                "id": f"strategy-{uuid4().hex[:10]}",
                "name": _duplicate_name(source.name, names),
                "version": 1,
            },
            deep=True,
        )
        return self.repository.create_strategy(duplicate)

    def update_strategy(self, strategy_id: str, request: VolumeStrategyInput) -> VolumeStrategy:
        current = self.get_strategy(strategy_id)
        updated_strategy = VolumeStrategy(
            id=current.id,
            owner_user_id=current.owner_user_id,
            version=current.version + 1,
            **request.model_dump(),
        )
        target_mode_changed = updated_strategy.target_mode is not current.target_mode
        direction_changed = updated_strategy.direction is not current.direction
        assigned = [instance for instance in self.repository.list() if instance.strategy_id == strategy_id]
        for instance in assigned:
            self._ensure_configurable(instance, "changing a shared strategy")
            projected_progress = (
                Decimal(0)
                if target_mode_changed or direction_changed
                else target_progress_quote(instance, updated_strategy)
            )
            if request.target_volume_quote < projected_progress:
                raise ValidationFailed(
                    f"strategy target cannot be lower than current target progress for instance {instance.id!r}"
                )

        projected = [
            self._strategy_projection(
                instance,
                updated_strategy,
                reset_progress=target_mode_changed or direction_changed,
            )
            for instance in assigned
        ]
        self.repository.replace_strategy_and_instances(updated_strategy, projected)
        for instance in projected:
            self._append_log(instance.id, LogLevel.INFO, f"共享策略已更新：{updated_strategy.name}")
        return updated_strategy

    def delete_strategy(self, strategy_id: str) -> None:
        self.get_strategy(strategy_id)
        assigned = [instance.id for instance in self.repository.list() if instance.strategy_id == strategy_id]
        if assigned:
            raise UnsafeOperation(f"strategy is assigned to {len(assigned)} instance(s)")
        self.repository.delete_strategy(strategy_id)

    def assign_strategy(self, strategy_id: str, instance_ids: list[str]) -> list[AccountInstance]:
        strategy = self.get_strategy(strategy_id)
        instances = [self.get_instance(instance_id) for instance_id in instance_ids]
        for instance in instances:
            self._ensure_configurable(instance, "assigning a strategy")
        updated = [self._strategy_projection(instance, strategy, reset_progress=True) for instance in instances]
        self.repository.replace_many(updated)
        for instance in updated:
            self._append_log(instance.id, LogLevel.INFO, f"已绑定共享策略：{strategy.name}；策略进度已重置")
        return updated


def _duplicate_name(source: str, existing: set[str]) -> str:
    index = 1
    while True:
        suffix = " 复制" if index == 1 else f" 复制 {index}"
        candidate = f"{source[: 64 - len(suffix)].rstrip()}{suffix}"
        if candidate.casefold() not in existing:
            return candidate
        index += 1

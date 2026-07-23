from __future__ import annotations

from threading import RLock

from .models import AccountInstance, LogLine, VolumeStrategy
from .ownership import current_owner_user_id
from .repository_shared import DuplicateInstanceError, DuplicateStrategyError


class InMemoryAccountRepository:
    def __init__(self) -> None:
        self._instances: dict[str, AccountInstance] = {}
        self._logs: dict[str, list[LogLine]] = {}
        self._log_reads: dict[str, int] = {}
        self._strategies: dict[str, VolumeStrategy] = {}
        self._lock = RLock()

    def list(self) -> list[AccountInstance]:
        with self._lock:
            owner = current_owner_user_id()
            return [
                instance.model_copy(deep=True)
                for instance in self._instances.values()
                if owner is None or instance.owner_user_id == owner
            ]

    def get(self, instance_id: str) -> AccountInstance | None:
        with self._lock:
            instance = self._instances.get(instance_id)
            owner = current_owner_user_id()
            if instance is None or (owner is not None and instance.owner_user_id != owner):
                return None
            return instance.model_copy(deep=True)

    def create(self, instance: AccountInstance) -> AccountInstance:
        with self._lock:
            owner = current_owner_user_id()
            if owner is not None and instance.owner_user_id != owner:
                raise PermissionError("cannot create an instance for another user")
            if instance.id in self._instances:
                raise DuplicateInstanceError(instance.id)
            self._instances[instance.id] = instance.model_copy(deep=True)
            self._logs[instance.id] = []
            self._log_reads[instance.id] = 0
            return instance.model_copy(deep=True)

    def replace(self, instance: AccountInstance) -> AccountInstance:
        with self._lock:
            current = self._instances.get(instance.id)
            owner = current_owner_user_id()
            if current is None or (owner is not None and current.owner_user_id != owner):
                raise KeyError(instance.id)
            self._instances[instance.id] = instance.model_copy(deep=True)
            return instance.model_copy(deep=True)

    def replace_many(self, instances: list[AccountInstance]) -> list[AccountInstance]:
        with self._lock:
            owner = current_owner_user_id()
            missing = [
                instance.id
                for instance in instances
                if instance.id not in self._instances
                or (owner is not None and self._instances[instance.id].owner_user_id != owner)
            ]
            if missing:
                raise KeyError(missing[0])
            for instance in instances:
                self._instances[instance.id] = instance.model_copy(deep=True)
            return [instance.model_copy(deep=True) for instance in instances]

    def delete(self, instance_id: str) -> None:
        with self._lock:
            instance = self._instances.get(instance_id)
            owner = current_owner_user_id()
            if instance is None or (owner is not None and instance.owner_user_id != owner):
                return
            self._instances.pop(instance_id, None)
            self._logs.pop(instance_id, None)
            self._log_reads.pop(instance_id, None)

    def append_log(self, instance_id: str, line: LogLine) -> None:
        with self._lock:
            if instance_id not in self._instances:
                raise KeyError(instance_id)
            self._logs[instance_id].append(line.model_copy(deep=True))
            self._logs[instance_id] = self._logs[instance_id][-500:]

    def read_logs(self, instance_id: str, limit: int) -> list[LogLine]:
        with self._lock:
            if instance_id not in self._instances:
                raise KeyError(instance_id)
            self._log_reads[instance_id] += 1
            return [line.model_copy(deep=True) for line in self._logs[instance_id][-limit:]]

    def clear_logs(self, instance_id: str) -> None:
        with self._lock:
            if instance_id not in self._instances:
                raise KeyError(instance_id)
            self._logs[instance_id] = []

    def log_read_count(self, instance_id: str) -> int:
        with self._lock:
            return self._log_reads.get(instance_id, 0)

    def list_strategies(self) -> list[VolumeStrategy]:
        with self._lock:
            owner = current_owner_user_id()
            return [
                strategy.model_copy(deep=True)
                for strategy in self._strategies.values()
                if owner is None or strategy.owner_user_id == owner
            ]

    def get_strategy(self, strategy_id: str) -> VolumeStrategy | None:
        with self._lock:
            strategy = self._strategies.get(strategy_id)
            owner = current_owner_user_id()
            if strategy is None or (owner is not None and strategy.owner_user_id != owner):
                return None
            return strategy.model_copy(deep=True)

    def create_strategy(self, strategy: VolumeStrategy) -> VolumeStrategy:
        with self._lock:
            owner = current_owner_user_id()
            if owner is not None and strategy.owner_user_id != owner:
                raise PermissionError("cannot create a strategy for another user")
            if strategy.id in self._strategies:
                raise DuplicateStrategyError(strategy.id)
            self._strategies[strategy.id] = strategy.model_copy(deep=True)
            return strategy.model_copy(deep=True)

    def replace_strategy_and_instances(
        self,
        strategy: VolumeStrategy,
        instances: list[AccountInstance],
    ) -> VolumeStrategy:
        with self._lock:
            owner = current_owner_user_id()
            current = self._strategies.get(strategy.id)
            if current is None or (owner is not None and current.owner_user_id != owner):
                raise KeyError(strategy.id)
            missing = [
                instance.id
                for instance in instances
                if instance.id not in self._instances
                or (owner is not None and self._instances[instance.id].owner_user_id != owner)
            ]
            if missing:
                raise KeyError(missing[0])
            self._strategies[strategy.id] = strategy.model_copy(deep=True)
            for instance in instances:
                self._instances[instance.id] = instance.model_copy(deep=True)
            return strategy.model_copy(deep=True)

    def delete_strategy(self, strategy_id: str) -> None:
        with self._lock:
            strategy = self._strategies.get(strategy_id)
            owner = current_owner_user_id()
            if strategy is not None and (owner is None or strategy.owner_user_id == owner):
                self._strategies.pop(strategy_id, None)

    def close(self) -> None:
        return None

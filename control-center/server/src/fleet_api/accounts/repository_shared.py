from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Protocol

from fleet_api.models import AccountInstance, LogLine, VolumeStrategy


class DuplicateInstanceError(ValueError):
    pass


class DuplicateStrategyError(ValueError):
    pass


class AccountRepository(Protocol):
    def list(self) -> list[AccountInstance]: ...

    def get(self, instance_id: str) -> AccountInstance | None: ...

    def create(self, instance: AccountInstance) -> AccountInstance: ...

    def replace(self, instance: AccountInstance) -> AccountInstance: ...

    def replace_many(self, instances: list[AccountInstance]) -> list[AccountInstance]: ...

    def delete(self, instance_id: str) -> None: ...

    def append_log(self, instance_id: str, line: LogLine) -> None: ...

    def read_logs(self, instance_id: str, limit: int) -> list[LogLine]: ...

    def clear_logs(self, instance_id: str, execution_boundaries: Mapping[str, int] | None = None) -> None: ...

    def log_clear_boundaries(self, instance_id: str) -> dict[str, int]: ...

    def log_read_count(self, instance_id: str) -> int: ...

    def list_strategies(self) -> list[VolumeStrategy]: ...

    def get_strategy(self, strategy_id: str) -> VolumeStrategy | None: ...

    def create_strategy(self, strategy: VolumeStrategy) -> VolumeStrategy: ...

    def replace_strategy_and_instances(
        self,
        strategy: VolumeStrategy,
        instances: list[AccountInstance],
    ) -> VolumeStrategy: ...

    def delete_strategy(self, strategy_id: str) -> None: ...

    def close(self) -> None: ...


def upgrade_instance_payload(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    volume = payload.get("volume")
    if isinstance(volume, dict):
        for key in ("session", "activeSession", "active_session", "lastRun", "last_run"):
            projection = volume.get(key)
            if not isinstance(projection, dict):
                continue
            strategy_target = projection.get(
                "strategyTargetQuoteVolume",
                projection.get("strategy_target_quote_volume"),
            )
            if strategy_target is not None:
                continue
            target = projection.get("targetQuoteVolume", projection.get("target_quote_volume"))
            if target is not None:
                projection["strategyTargetQuoteVolume"] = target
    strategy = payload.get("strategy")
    if not isinstance(strategy, dict):
        return payload
    strategy_id = strategy.get("id")
    if strategy_id and not payload.get("strategyId"):
        payload["strategyId"] = strategy_id
    if "roundTurnoverQuoteMin" in strategy:
        return payload

    minimum = strategy.pop("btcOpenQuoteMin", None)
    maximum = strategy.pop("btcOpenQuoteMax", None)
    reference_beta = strategy.pop("referenceEthRatio", "0.25")
    if minimum is None or maximum is None:
        return payload
    beta = Decimal(str(reference_beta))
    multiplier = (Decimal(1) + beta) * Decimal(2)
    strategy["roundTurnoverQuoteMin"] = str(Decimal(str(minimum)) * multiplier)
    strategy["roundTurnoverQuoteMax"] = str(Decimal(str(maximum)) * multiplier)
    return payload

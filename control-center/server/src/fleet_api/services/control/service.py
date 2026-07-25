from __future__ import annotations

from decimal import Decimal

from fleet_api.accounts.repository import AccountRepository
from fleet_api.auth.vault import CredentialVault
from fleet_api.services.control.service_config import ServiceConfigMixin
from fleet_api.services.control.service_errors import (  # noqa: F401
    BetaSourceUnavailable,
    FleetError,
    InstanceNotFound,
    StrategyNotFound,
    TelemetryUnavailable,
    UnsafeOperation,
    ValidationFailed,
)
from fleet_api.services.operations.service_execution import ServiceExecutionMixin
from fleet_api.services.operations.service_instances import ServiceInstancesMixin
from fleet_api.services.operations.service_logs import ServiceLogsMixin
from fleet_api.services.operations.service_strategy import ServiceStrategyMixin
from fleet_api.services.operations.service_telemetry import ServiceTelemetryMixin


class FleetControlService(
    ServiceStrategyMixin,
    ServiceInstancesMixin,
    ServiceExecutionMixin,
    ServiceConfigMixin,
    ServiceTelemetryMixin,
    ServiceLogsMixin,
):
    def __init__(
        self,
        repository: AccountRepository,
        vault: CredentialVault,
        *,
        adapter: str = "mock",
        mock_cycle_total_quote: Decimal = Decimal("20"),
    ) -> None:
        if adapter not in {"mock", "weex-readonly", "weex-live"}:
            raise ValueError("unsupported control-plane adapter")
        if not mock_cycle_total_quote.is_finite() or mock_cycle_total_quote <= 0:
            raise ValueError("mock cycle total quote must be finite and positive")
        self.repository = repository
        self.vault = vault
        self.adapter = adapter
        self.mock_cycle_total_quote = mock_cycle_total_quote
        self._reconcile_strategy_catalog()

    def list_instances(self):
        return self.repository.list()

    def get_instance(self, instance_id: str):
        instance = self.repository.get(instance_id)
        if instance is None:
            raise InstanceNotFound(f"instance {instance_id!r} was not found")
        return instance

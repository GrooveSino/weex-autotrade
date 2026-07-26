from decimal import Decimal

from fleet_api.accounts.repository import InMemoryAccountRepository
from fleet_api.auth.vault import EphemeralCredentialVault
from fleet_api.models import (
    AccountInstance,
    ExposureSnapshot,
    InstanceStatus,
    ProxySnapshot,
    ProxyStatus,
    ProxyType,
    TradingMode,
    VolumeSnapshot,
    WalletSnapshot,
)
from fleet_api.runtime.telemetry import AccountTelemetry
from fleet_api.services.control.service import FleetControlService
from fleet_api.volume.core.volume_history import TradeVolumeAggregate


def _account() -> AccountInstance:
    return AccountInstance(
        id="ins-volume-projection",
        name="Volume account",
        account_tag="report",
        api_key_tail="ABCD",
        mode=TradingMode.LIVE,
        status=InstanceStatus.STOPPED,
        phase="已停止",
        proxy=ProxySnapshot(type=ProxyType.HTTPS, host="proxy.example:8080"),
    )


def test_stale_telemetry_cannot_erase_a_committed_manual_history_total() -> None:
    repository = InMemoryAccountRepository()
    stored = repository.create(_account())
    service = FleetControlService(repository, EphemeralCredentialVault())
    service.apply_volume_aggregate(
        stored.id,
        TradeVolumeAggregate(Decimal("100"), Decimal("25"), 3, False),
    )

    service.apply_telemetry(
        stored.id,
        AccountTelemetry(
            wallet=WalletSnapshot(equity=50, available=40),
            volume=VolumeSnapshot(lifetime=0, today=0, complete=False),
            exposure=ExposureSnapshot(),
            cycle_completed=0,
            proxy_status=ProxyStatus.HEALTHY,
            proxy_latency_ms=20,
            proxy_location="test",
            phase="已同步",
        ),
    )

    updated = service.get_instance(stored.id)
    assert (updated.volume.lifetime, updated.volume.today, updated.volume.complete) == (100, 25, False)
    assert updated.wallet.available == 40

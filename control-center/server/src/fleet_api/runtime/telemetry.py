from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Protocol

from fleet_api.auth.vault import CredentialMaterial
from fleet_api.models import (
    AccountInstance,
    ExposureSnapshot,
    InstanceStatus,
    ProxyStatus,
    TradingMode,
    VolumeSnapshot,
    WalletSnapshot,
)


@dataclass(frozen=True, slots=True)
class AccountTelemetryContext:
    instance: AccountInstance
    credentials: CredentialMaterial | None


@dataclass(frozen=True, slots=True)
class AccountTelemetry:
    wallet: WalletSnapshot
    volume: VolumeSnapshot
    exposure: ExposureSnapshot
    cycle_completed: int
    proxy_status: ProxyStatus
    proxy_latency_ms: int | None
    proxy_location: str
    phase: str
    activity_log: str | None = None


class AccountTelemetryAdapter(Protocol):
    async def collect(self, context: AccountTelemetryContext) -> AccountTelemetry: ...

    async def aclose(self) -> None: ...


class AccountTelemetryAdapterFactory(Protocol):
    def create(self, instance_id: str) -> AccountTelemetryAdapter: ...


class MockLiveTelemetryUnavailable(RuntimeError):
    pass


class MockAccountTelemetryAdapter:
    """Account-scoped simulator. It never creates a network client or submits an order."""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    async def collect(self, context: AccountTelemetryContext) -> AccountTelemetry:
        instance = context.instance
        if instance.mode is TradingMode.LIVE:
            raise MockLiveTelemetryUnavailable("the mock adapter cannot read a Live account")
        running = instance.status is InstanceStatus.RUNNING
        base_equity = instance.wallet.equity or self._rng.uniform(900, 1400)
        pnl_delta = self._rng.uniform(-0.8, 0.8) if running else 0
        equity = max(0, base_equity + pnl_delta)
        available = instance.wallet.available
        if available == 0 and instance.wallet.equity == 0:
            available = equity * 0.82
        elif running:
            available = max(0, min(equity, available + pnl_delta))

        return AccountTelemetry(
            wallet=WalletSnapshot(
                equity=equity,
                available=available,
                unrealized_pnl=instance.wallet.unrealized_pnl + pnl_delta,
            ),
            volume=VolumeSnapshot(
                lifetime=instance.volume.lifetime,
                today=instance.volume.today,
                complete=instance.volume.complete,
            ),
            exposure=instance.exposure,
            cycle_completed=instance.cycle.completed,
            proxy_status=ProxyStatus.HEALTHY,
            proxy_latency_ms=instance.proxy.latency_ms or self._rng.randint(60, 160),
            proxy_location="Mock / account-bound",
            phase="Mock 遥测已同步" if running else instance.phase,
        )

    async def aclose(self) -> None:
        return None


class MockAccountTelemetryAdapterFactory:
    def create(self, instance_id: str) -> AccountTelemetryAdapter:
        digest = hashlib.sha256(instance_id.encode()).digest()
        return MockAccountTelemetryAdapter(int.from_bytes(digest[:8]))

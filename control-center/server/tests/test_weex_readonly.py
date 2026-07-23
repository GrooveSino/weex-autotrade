from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from pydantic import SecretStr

from fleet_api.models import (
    AccountInstance,
    InstanceStatus,
    ProxySnapshot,
    ProxyType,
    TradingMode,
)
from fleet_api.telemetry import AccountTelemetryContext
from fleet_api.vault import CredentialMaterial
from fleet_api.volume_history import InMemoryTradeVolumeLedger
from fleet_api.weex_readonly import (
    DAY_MS,
    MissingAccountCredentials,
    ReadonlyLiveAccountRequired,
    WeexReadonlyAccountTelemetryAdapterFactory,
)


def account(instance_id: str = "ins-live", mode: TradingMode = TradingMode.LIVE) -> AccountInstance:
    return AccountInstance(
        id=instance_id,
        name="Live account",
        account_tag="readonly",
        api_key_tail="ABCD",
        mode=mode,
        status=InstanceStatus.STOPPED,
        phase="等待同步",
        proxy=ProxySnapshot(type=ProxyType.HTTPS, host="proxy.example:8080"),
    )


def material(proxy_url: str = "http://user:pass@proxy.example:8080") -> CredentialMaterial:
    return CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=SecretStr(proxy_url),
    )


class FakeReadonlyGateway:
    def __init__(self) -> None:
        self.closed = False
        self.trade_calls: list[tuple[int, int, int]] = []
        self.fill_time = time.time_ns() // 1_000_000 - 1_000

    def account_balance_rows(self, mode: str) -> list[dict[str, Any]]:
        assert mode == "live"
        return [
            {
                "asset": "USDT",
                "balance": "100.25",
                "availableBalance": "82.50",
                "unrealizePnl": "-2.25",
            }
        ]

    def all_position_rows(self, mode: str) -> list[dict[str, Any]]:
        assert mode == "live"
        return [
            {"symbol": "BTCUSDT", "side": "LONG", "openValue": "123.45"},
            {"symbol": "BTCUSDT", "side": "SHORT", "openValue": "8"},
            {"symbol": "ETHUSDT", "side": "SHORT", "openValue": "67.89"},
        ]

    def trade_rows(
        self,
        mode: str,
        symbol: str | None,
        *,
        start_time: int,
        end_time: int,
        limit: int,
        page: int | None = None,
    ) -> list[dict[str, Any]]:
        assert mode == "live"
        assert symbol is None
        assert page is None
        self.trade_calls.append((start_time, end_time, limit))
        if not start_time <= self.fill_time <= end_time:
            return []
        return [
            {
                "id": "fill-1",
                "orderId": "order-1",
                "symbol": "BTCUSDT",
                "price": "50000",
                "qty": "0.001",
                "quoteQty": "50.125",
                "time": self.fill_time,
            },
            {
                "id": "fill-2",
                "orderId": "order-2",
                "symbol": "ETHUSDT",
                "price": "2500",
                "qty": "0.01",
                "quoteQty": "25.25",
                "time": self.fill_time,
            },
        ]

    def close(self) -> None:
        self.closed = True


class HistoryFailingGateway(FakeReadonlyGateway):
    def trade_rows(
        self,
        mode: str,
        symbol: str | None,
        *,
        start_time: int,
        end_time: int,
        limit: int,
        page: int | None = None,
    ) -> list[dict[str, Any]]:
        raise IndexError("fixture-only malformed history response")


def test_live_readonly_adapter_maps_wallet_positions_and_exact_fill_volume() -> None:
    async def scenario() -> None:
        ledger = InMemoryTradeVolumeLedger()
        gateway = FakeReadonlyGateway()
        created_with: list[tuple[CredentialMaterial, int]] = []

        def gateway_factory(credentials: CredentialMaterial, timeout_ms: int):
            created_with.append((credentials, timeout_ms))
            return gateway

        adapter = WeexReadonlyAccountTelemetryAdapterFactory(
            ledger,
            request_timeout_ms=3_000,
            history_lookback_days=1,
            gateway_factory=gateway_factory,
        ).create("ins-live")
        credentials = material()
        telemetry = await adapter.collect(AccountTelemetryContext(account(), credentials))

        assert telemetry.wallet.equity == 98
        assert telemetry.wallet.available == 82.5
        assert telemetry.wallet.unrealized_pnl == -2.25
        assert telemetry.exposure.btc_long == 123.45
        assert telemetry.exposure.eth_short == 67.89
        assert telemetry.volume.lifetime == 0
        assert telemetry.volume.today == 0
        assert telemetry.volume.complete is False
        assert telemetry.proxy_location == "WEEX / account-bound"
        assert telemetry.phase == "WEEX 只读遥测已同步"
        assert telemetry.activity_log is None
        assert created_with == [(credentials, 3_000)]
        assert gateway.trade_calls == []

        result = await adapter.sync_history_step(
            AccountTelemetryContext(account(), credentials),
            now_ms=time.time_ns() // 1_000_000,
        )
        refreshed = await adapter.collect(AccountTelemetryContext(account(), credentials))

        assert result.fills_inserted == 2
        assert refreshed.volume.lifetime == 75.375
        assert refreshed.volume.today == 75.375
        assert len(gateway.trade_calls) == 1

        await adapter.aclose()
        assert gateway.closed is True

    asyncio.run(scenario())


def test_configured_history_start_within_retention_marks_volume_complete() -> None:
    async def scenario() -> None:
        now_ms = time.time_ns() // 1_000_000
        instance = account().model_copy(update={"history_start_at_ms": now_ms - 2 * 60 * 60 * 1000})
        adapter = WeexReadonlyAccountTelemetryAdapterFactory(
            InMemoryTradeVolumeLedger(),
            history_lookback_days=1,
            gateway_factory=lambda credentials, timeout_ms: FakeReadonlyGateway(),
        ).create(instance.id)

        context = AccountTelemetryContext(instance, material())
        telemetry = await adapter.collect(context)
        await adapter.sync_history_step(context, now_ms=now_ms)
        telemetry = await adapter.collect(context)

        assert telemetry.volume.complete is True
        assert telemetry.phase == "WEEX 只读遥测已同步"

    asyncio.run(scenario())


def test_history_start_older_than_retention_is_clamped_and_explained() -> None:
    async def scenario() -> None:
        now_ms = time.time_ns() // 1_000_000
        instance = account().model_copy(update={"history_start_at_ms": now_ms - 2 * DAY_MS})
        adapter = WeexReadonlyAccountTelemetryAdapterFactory(
            InMemoryTradeVolumeLedger(),
            history_lookback_days=1,
            gateway_factory=lambda credentials, timeout_ms: FakeReadonlyGateway(),
        ).create(instance.id)

        context = AccountTelemetryContext(instance, material())
        telemetry = await adapter.collect(context)
        await adapter.sync_history_step(context, now_ms=now_ms)
        telemetry = await adapter.collect(context)

        assert telemetry.volume.complete is False
        assert telemetry.phase == "WEEX 只读遥测已同步"

    asyncio.run(scenario())


def test_history_failure_is_isolated_from_wallet_and_position_telemetry() -> None:
    async def scenario() -> None:
        adapter = WeexReadonlyAccountTelemetryAdapterFactory(
            InMemoryTradeVolumeLedger(),
            history_lookback_days=1,
            gateway_factory=lambda credentials, timeout_ms: HistoryFailingGateway(),
        ).create("ins-live")

        context = AccountTelemetryContext(account(), material())
        first = await adapter.collect(context)
        with pytest.raises(IndexError):
            await adapter.sync_history_step(context, now_ms=time.time_ns() // 1_000_000)
        second = await adapter.collect(context)

        assert first.wallet.equity == 98
        assert first.exposure.btc_long == 123.45
        assert first.exposure.eth_short == 67.89
        assert first.volume.complete is False
        assert first.phase == "WEEX 只读遥测已同步"
        assert first.activity_log is None
        assert second.activity_log is None

    asyncio.run(scenario())




def test_each_account_gets_an_independent_gateway_and_proxy_material() -> None:
    async def scenario() -> None:
        ledger = InMemoryTradeVolumeLedger()
        gateways: list[FakeReadonlyGateway] = []
        proxy_urls: list[str] = []

        def gateway_factory(credentials: CredentialMaterial, timeout_ms: int):
            assert timeout_ms == 15_000
            proxy_urls.append(credentials.proxy_url.get_secret_value())
            gateway = FakeReadonlyGateway()
            gateways.append(gateway)
            return gateway

        factory = WeexReadonlyAccountTelemetryAdapterFactory(
            ledger,
            history_lookback_days=1,
            gateway_factory=gateway_factory,
        )
        first = factory.create("ins-one")
        second = factory.create("ins-two")
        await asyncio.gather(
            first.collect(
                AccountTelemetryContext(account("ins-one"), material("http://one:pass@proxy-one.example:8001"))
            ),
            second.collect(
                AccountTelemetryContext(account("ins-two"), material("socks5://two:pass@proxy-two.example:1080"))
            ),
        )

        assert proxy_urls == [
            "http://one:pass@proxy-one.example:8001",
            "socks5://two:pass@proxy-two.example:1080",
        ]
        assert len(gateways) == 2
        assert gateways[0] is not gateways[1]
        await asyncio.gather(first.aclose(), second.aclose())
        assert all(gateway.closed for gateway in gateways)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("context", "error"),
    [
        (AccountTelemetryContext(account(mode=TradingMode.DEMO), material()), ReadonlyLiveAccountRequired),
        (AccountTelemetryContext(account(), None), MissingAccountCredentials),
    ],
)
def test_invalid_readonly_context_is_rejected_before_gateway_creation(context, error) -> None:
    async def scenario() -> None:
        calls = 0

        def gateway_factory(credentials: CredentialMaterial, timeout_ms: int):
            nonlocal calls
            calls += 1
            return FakeReadonlyGateway()

        adapter = WeexReadonlyAccountTelemetryAdapterFactory(
            InMemoryTradeVolumeLedger(),
            gateway_factory=gateway_factory,
        ).create(context.instance.id)
        with pytest.raises(error):
            await adapter.collect(context)
        assert calls == 0

    asyncio.run(scenario())

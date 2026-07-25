from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from fleet_api.auth.vault import CredentialMaterial
from fleet_api.bootstrap.main_context import FleetAppContext
from fleet_api.models import AccountInstance, InstanceStatus, ProxySnapshot, ProxyType, TradingMode
from fleet_api.transport.routes.main_routes_volume import register_trade_volume_report_routes
from fleet_api.volume.reports import AccountTradeVolumeReportError, AccountTradeVolumeReportService


def account(mode: TradingMode = TradingMode.LIVE) -> AccountInstance:
    return AccountInstance(
        id="ins-volume",
        name="Volume account",
        account_tag="report",
        api_key_tail="ABCD",
        mode=mode,
        status=InstanceStatus.STOPPED,
        phase="已停止",
        proxy=ProxySnapshot(type=ProxyType.HTTPS, host="proxy.example:8080"),
    )


def material() -> CredentialMaterial:
    return CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=SecretStr("http://user:pass@proxy.example:8080"),
    )


class ReportGateway:
    def __init__(self) -> None:
        self.closed = False
        self.trade_calls = 0
        self.fill_time = time.time_ns() // 1_000_000

    def trade_rows(
        self,
        mode: str,
        symbol: str | None,
        *,
        start_time: int,
        end_time: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        assert mode == "live"
        assert symbol is None
        assert limit == 100
        self.trade_calls += 1
        if not start_time <= self.fill_time <= end_time:
            return []
        return [
            {
                "id": f"maker-{self.trade_calls}",
                "orderId": "order-maker",
                "symbol": "BTCUSDT",
                "price": "50000",
                "qty": "0.001",
                "quoteQty": "50.125",
                "maker": True,
                "time": self.fill_time,
            },
            {
                "id": f"taker-{self.trade_calls}",
                "orderId": "order-taker",
                "symbol": "ETHUSDT",
                "price": "2500",
                "qty": "0.01",
                "quoteQty": "25.25",
                "maker": False,
                "time": self.fill_time,
            },
        ]

    def close(self) -> None:
        self.closed = True


def test_report_uses_actual_quote_qty_and_never_returns_trade_rows() -> None:
    async def scenario() -> None:
        gateways: list[ReportGateway] = []
        received: list[CredentialMaterial] = []

        def factory(credentials: CredentialMaterial, timeout_ms: int) -> ReportGateway:
            assert timeout_ms == 3_000
            received.append(credentials)
            gateway = ReportGateway()
            gateways.append(gateway)
            return gateway

        service = AccountTradeVolumeReportService(factory, request_timeout_ms=3_000)
        response = await service.report(account(), material(), [7, 1, 7])

        assert [period.lookback_days for period in response.periods] == [1, 7]
        assert all(str(period.total_quote_volume) == "75.375" for period in response.periods)
        assert all(str(period.maker_quote_volume) == "50.125" for period in response.periods)
        assert all(str(period.taker_quote_volume) == "25.25" for period in response.periods)
        assert all(period.trade_count == 2 and period.complete for period in response.periods)
        assert all(gateway.closed for gateway in gateways)
        assert received == [material()]
        assert "trades" not in response.model_dump()
        assert "orderId" not in str(response.model_dump())

    asyncio.run(scenario())


def test_report_rejects_demo_and_missing_credentials_with_actionable_chinese_errors() -> None:
    async def scenario() -> None:
        service = AccountTradeVolumeReportService(lambda *_: ReportGateway(), request_timeout_ms=3_000)
        for instance, credentials, expected_code in (
            (account(TradingMode.DEMO), material(), "live_account_required"),
            (account(), None, "credentials_missing"),
        ):
            try:
                await service.report(instance, credentials, [1])
            except AccountTradeVolumeReportError as exc:
                assert exc.code == expected_code
                assert "。" in exc.message
                assert exc.action
            else:
                raise AssertionError("expected an actionable report error")

    asyncio.run(scenario())


def test_route_returns_camel_case_periods_and_safe_errors() -> None:
    gateway = ReportGateway()
    report_service = AccountTradeVolumeReportService(lambda *_: gateway, request_timeout_ms=3_000)
    stored = account()
    ctx = FleetAppContext(
        selected=SimpleNamespace(adapter="weex-readonly"),
        service=SimpleNamespace(get_instance=lambda instance_id: stored),
        vault=SimpleNamespace(get=lambda instance_id: material()),
        account_trade_volume_report_service=report_service,
    )
    app = FastAPI()
    register_trade_volume_report_routes(app, ctx)

    with TestClient(app) as api:
        response = api.get("/api/v1/instances/ins-volume/trade-volume-report?lookback_days=1&lookback_days=7")
        assert response.status_code == 200
        payload = response.json()
        assert [period["lookbackDays"] for period in payload["periods"]] == [1, 7]
        assert payload["periods"][0]["totalQuoteVolume"] == "75.375"
        assert "trades" not in str(payload)

        invalid = api.get("/api/v1/instances/ins-volume/trade-volume-report?lookback_days=2")
        assert invalid.status_code == 422
        assert "仅支持近 1 天、近 7 天或近 30 天" in invalid.json()["detail"]
        assert "下一步：" in invalid.json()["detail"]

    assert gateway.closed


def test_same_account_report_calls_are_serialized() -> None:
    class SlowGateway(ReportGateway):
        active = 0
        peak = 0

        def trade_rows(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            type(self).active += 1
            type(self).peak = max(type(self).peak, type(self).active)
            time.sleep(0.02)
            try:
                return super().trade_rows(*args, **kwargs)
            finally:
                type(self).active -= 1

    async def scenario() -> None:
        service = AccountTradeVolumeReportService(lambda *_: SlowGateway(), request_timeout_ms=3_000)
        await asyncio.gather(
            service.report(account(), material(), [1]),
            service.report(account(), material(), [1]),
        )
        assert SlowGateway.peak == 1

    asyncio.run(scenario())

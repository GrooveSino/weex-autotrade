from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import SecretStr

from fleet_api.auth.vault import CredentialMaterial
from fleet_api.market.weex_readonly import WeexLiveTradeHistorySource
from fleet_api.models import AccountInstance, InstanceStatus, ProxySnapshot, ProxyType, TradingMode
from fleet_api.volume.core.volume_history import TradeHistoryContext


def account() -> AccountInstance:
    return AccountInstance(
        id="ins-live",
        name="Live account",
        account_tag="readonly",
        api_key_tail="ABCD",
        mode=TradingMode.LIVE,
        status=InstanceStatus.STOPPED,
        phase="等待同步",
        proxy=ProxySnapshot(type=ProxyType.HTTPS, host="proxy.example:8080"),
    )


def material() -> CredentialMaterial:
    return CredentialMaterial(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        passphrase=SecretStr("passphrase"),
        proxy_url=SecretStr("http://user:pass@proxy.example:8080"),
    )


class SplittingGateway:
    def __init__(self) -> None:
        self.trade_calls: list[tuple[int, int, int]] = []

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
        self.trade_calls.append((start_time, end_time, limit))
        if end_time - start_time > 1_000:
            return [{} for _ in range(limit)]
        return [
            {
                "id": f"fill-{start_time}",
                "orderId": f"order-{start_time}",
                "symbol": "BTCUSDT",
                "price": "1",
                "qty": "1",
                "quoteQty": "1",
                "time": start_time,
            }
        ]


class FailOnceGateway(SplittingGateway):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def trade_rows(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        start_time = int(kwargs["start_time"])
        end_time = int(kwargs["end_time"])
        limit = int(kwargs["limit"])
        self.trade_calls.append((start_time, end_time, limit))
        if not self.failed:
            self.failed = True
            raise TimeoutError("fixture timeout")
        return []


def test_hundred_row_window_split_survives_source_restore_without_duplicate_fills() -> None:
    async def scenario() -> None:
        gateway = SplittingGateway()
        context = TradeHistoryContext(account(), material())
        source = WeexLiveTradeHistorySource(gateway)
        source.begin(0, 3_999, coverage_complete=True)
        first = await source.fetch_page(context, cursor=None, limit=100)
        assert first.fills == ()

        resumed = WeexLiveTradeHistorySource(gateway)
        assert resumed.restore(source.snapshot()) is True
        cursor = first.next_cursor
        fills = []
        while cursor is not None:
            page = await resumed.fetch_page(context, cursor=cursor, limit=100)
            fills.extend(page.fills)
            cursor = page.next_cursor

        assert [fill.identity for fill in fills] == ["fill-0", "fill-1000", "fill-2000", "fill-3000"]
        assert len(gateway.trade_calls) == 7

    asyncio.run(scenario())


def test_failed_window_remains_in_checkpoint_and_is_retried_exactly() -> None:
    async def scenario() -> None:
        gateway = FailOnceGateway()
        context = TradeHistoryContext(account(), material())
        source = WeexLiveTradeHistorySource(gateway)
        source.begin(10, 20, coverage_complete=True)

        with pytest.raises(TimeoutError):
            await source.fetch_page(context, cursor=None, limit=100)
        assert source.snapshot()["pending_windows"] == [[10, 20]]

        page = await source.fetch_page(context, cursor=None, limit=100)
        assert page.complete is True
        assert gateway.trade_calls == [(10, 20, 100), (10, 20, 100)]

    asyncio.run(scenario())

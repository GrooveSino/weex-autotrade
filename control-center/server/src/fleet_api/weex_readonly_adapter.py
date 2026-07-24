from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from pydantic import SecretStr

from .models import ProxyStatus, TradingMode, VolumeSnapshot
from .telemetry import AccountTelemetry, AccountTelemetryAdapter, AccountTelemetryContext
from .volume_history import (
    InMemoryTradeVolumeLedger,
    TradeHistoryContext,
    TradeHistorySynchronizer,
    TradeVolumeLedger,
    shanghai_day_start_ms,
)
from .weex_readonly import (
    DAY_MS,
    HISTORY_OVERLAP_MS,
    GatewayFactory,
    MissingAccountCredentials,
    ReadonlyLiveAccountRequired,
    ReadonlyWeexGateway,
    WeexLiveTradeHistorySource,
    _exposure_snapshot,
    _finite_float,
    _wallet_snapshot,
)


class WeexReadonlyAccountTelemetryAdapter(AccountTelemetryAdapter):
    def __init__(
        self,
        ledger: TradeVolumeLedger,
        gateway_factory: GatewayFactory,
        *,
        request_timeout_ms: int,
        history_lookback_days: int,
        history_pages_per_poll: int,
    ) -> None:
        self._ledger = ledger
        self._gateway_factory = gateway_factory
        self._request_timeout_ms = request_timeout_ms
        self._history_lookback_ms = history_lookback_days * DAY_MS
        self._history_synchronizer = TradeHistorySynchronizer(ledger, page_size=100, max_pages=history_pages_per_poll)
        self._gateway: ReadonlyWeexGateway | None = None

    async def collect(self, context: AccountTelemetryContext) -> AccountTelemetry:
        self._require_live_credentials(context)
        self._ensure_gateway(context)
        started = time.perf_counter()
        balance_rows, position_rows = await asyncio.to_thread(self._read_snapshot)
        wallet = _wallet_snapshot(balance_rows)
        exposure = _exposure_snapshot(position_rows)
        now_ms = time.time_ns() // 1_000_000
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        aggregate = self._ledger.aggregate(context.instance.id, shanghai_day_start_ms(now_ms))
        return AccountTelemetry(
            wallet=wallet,
            volume=VolumeSnapshot(
                lifetime=_finite_float(aggregate.lifetime, "lifetime volume"),
                today=_finite_float(aggregate.today, "today volume"),
                complete=aggregate.complete,
                session=self._ledger.latest_session(context.instance.id, context.instance.mode.value),
            ),
            exposure=exposure,
            cycle_completed=context.instance.cycle.completed,
            proxy_status=ProxyStatus.HEALTHY,
            proxy_latency_ms=latency_ms,
            proxy_location="WEEX / account-bound",
            phase="WEEX 只读遥测已同步",
            activity_log=None,
        )

    async def aclose(self) -> None:
        gateway = self._gateway
        self._gateway = None
        if gateway is not None:
            await asyncio.to_thread(gateway.close)

    async def authoritative_fills(self, context: AccountTelemetryContext, *, start_ms: int, end_ms: int):
        self._require_live_credentials(context)
        self._ensure_gateway(context)
        assert self._gateway is not None
        source = WeexLiveTradeHistorySource(self._gateway)
        source.begin(start_ms, end_ms, coverage_complete=True)
        temporary = InMemoryTradeVolumeLedger()
        result = await TradeHistorySynchronizer(temporary, page_size=100, max_pages=1_000).sync(
            context.instance.id,
            TradeHistoryContext(context.instance, context.credentials),
            source,
            today_start_ms=start_ms,
            coverage_start_ms=start_ms,
        )
        fills = temporary.fills_for_account(context.instance.id, context.instance.mode.value, start_ms)
        return fills, result.stop_reason == "history_exhausted" and result.aggregate.complete, result.stop_reason

    async def sync_history_step(self, context: AccountTelemetryContext, *, now_ms: int):
        """Run one persisted window/page without coupling it to telemetry."""
        self._require_live_credentials(context)
        self._ensure_gateway(context)
        assert self._gateway is not None
        checkpoint = self._ledger.sync_checkpoint(context.instance.id, context.instance.mode.value) or {}
        source = WeexLiveTradeHistorySource(self._gateway)
        restored = isinstance(checkpoint.get("scan_state"), dict) and source.restore(checkpoint["scan_state"])
        if restored:
            cursor = checkpoint.get("cursor") if isinstance(checkpoint.get("cursor"), str) else None
            coverage_start_ms = source.scan_start_ms
        else:
            cursor = None
            coverage_start_ms, coverage_complete = self._history_scan_start(context, checkpoint, now_ms)
            source.begin(coverage_start_ms, now_ms, coverage_complete=coverage_complete)
        return await self._history_synchronizer.step(
            context.instance.id,
            TradeHistoryContext(context.instance, context.credentials),
            source,
            today_start_ms=shanghai_day_start_ms(now_ms),
            cursor=cursor,
            coverage_start_ms=coverage_start_ms,
        )

    def _require_live_credentials(self, context: AccountTelemetryContext) -> None:
        if context.instance.mode is not TradingMode.LIVE:
            raise ReadonlyLiveAccountRequired("WEEX read-only telemetry currently supports Live accounts")
        if context.credentials is None:
            raise MissingAccountCredentials("account credentials are unavailable")

    def _ensure_gateway(self, context: AccountTelemetryContext) -> None:
        if self._gateway is not None:
            return
        assert context.credentials is not None
        credentials = context.credentials
        proxy_url = credentials.proxy_url.get_secret_value() if credentials.proxy_url is not None else None
        if proxy_url is not None and "://" not in proxy_url:
            credentials = replace(
                credentials, proxy_url=SecretStr(f"{context.instance.proxy.type.value}://{proxy_url}")
            )
        self._gateway = self._gateway_factory(credentials, self._request_timeout_ms)

    def _read_snapshot(self):
        assert self._gateway is not None
        return self._gateway.account_balance_rows("live"), self._gateway.all_position_rows("live")

    def _history_scan_start(
        self,
        context: AccountTelemetryContext,
        checkpoint: dict[str, object],
        now_ms: int,
    ) -> tuple[int, bool]:
        watermark = checkpoint.get("high_watermark_ms")
        if isinstance(watermark, int) and watermark > 0:
            return max(0, watermark - HISTORY_OVERLAP_MS), bool(checkpoint.get("coverage_complete"))
        retention_start = max(0, now_ms - self._history_lookback_ms + 1)
        configured_start = context.instance.history_start_at_ms
        start_ms = max(retention_start, configured_start) if configured_start is not None else retention_start
        return start_ms, configured_start is not None and configured_start >= retention_start


class WeexReadonlyAccountTelemetryAdapterFactory:
    def __init__(
        self,
        ledger: TradeVolumeLedger,
        *,
        request_timeout_ms: int = 15_000,
        history_lookback_days: int = 365,
        history_pages_per_poll: int = 1,
        gateway_factory: GatewayFactory | None = None,
    ) -> None:
        if request_timeout_ms < 1_000:
            raise ValueError("WEEX request timeout must be at least 1000ms")
        if not 1 <= history_lookback_days <= 365:
            raise ValueError("WEEX history lookback must be between 1 and 365 days")
        if history_pages_per_poll < 1:
            raise ValueError("WEEX history pages per poll must be at least 1")
        self._ledger = ledger
        self._request_timeout_ms = request_timeout_ms
        self._history_lookback_days = history_lookback_days
        self._history_pages_per_poll = history_pages_per_poll
        if gateway_factory is None:
            from .weex_readonly import build_readonly_gateway

            gateway_factory = build_readonly_gateway
        self._gateway_factory = gateway_factory

    def create(self, instance_id: str) -> AccountTelemetryAdapter:
        del instance_id
        return WeexReadonlyAccountTelemetryAdapter(
            self._ledger,
            self._gateway_factory,
            request_timeout_ms=self._request_timeout_ms,
            history_lookback_days=self._history_lookback_days,
            history_pages_per_poll=self._history_pages_per_poll,
        )

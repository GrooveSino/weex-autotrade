from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from pydantic import SecretStr

from .models import ProxyStatus, TradingMode, VolumeSnapshot
from .telemetry import AccountTelemetry, AccountTelemetryAdapter, AccountTelemetryContext
from .volume_history import InMemoryTradeVolumeLedger, TradeHistoryContext, TradeHistorySynchronizer, TradeVolumeLedger, shanghai_day_start_ms
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
        self._history_source: WeexLiveTradeHistorySource | None = None
        self._history_cursor: str | None = None
        self._history_scan_active = False
        self._history_scan_end_ms: int | None = None
        self._history_scan_start_ms: int | None = None
        self._last_history_end_ms: int | None = None
        self._history_coverage_complete = False
        self._history_coverage_reason: str | None = None
        self._last_history_failure_type: str | None = None

    async def collect(self, context: AccountTelemetryContext) -> AccountTelemetry:
        self._require_live_credentials(context)
        self._ensure_gateway(context)
        started = time.perf_counter()
        balance_rows, position_rows = await asyncio.to_thread(self._read_snapshot)
        wallet = _wallet_snapshot(balance_rows)
        exposure = _exposure_snapshot(position_rows)
        now_ms = time.time_ns() // 1_000_000
        history_result, history_failure = await self._collect_history(context, now_ms)
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        aggregate = self._ledger.aggregate(context.instance.id, shanghai_day_start_ms(now_ms))
        return AccountTelemetry(
            wallet=wallet,
            volume=VolumeSnapshot(
                lifetime=_finite_float(aggregate.lifetime, "lifetime volume"),
                today=_finite_float(aggregate.today, "today volume"),
                complete=aggregate.complete and history_failure is None,
                session=self._ledger.latest_session(context.instance.id, context.instance.mode.value),
            ),
            exposure=exposure,
            cycle_completed=context.instance.cycle.completed,
            proxy_status=ProxyStatus.HEALTHY,
            proxy_latency_ms=latency_ms,
            proxy_location="WEEX / account-bound",
            phase=self._phase(history_failure),
            activity_log=self._activity_log(history_result, history_failure),
        )

    async def aclose(self) -> None:
        gateway = self._gateway
        self._gateway = None
        self._history_source = None
        if gateway is not None:
            await asyncio.to_thread(gateway.close)

    async def authoritative_fills(self, context: AccountTelemetryContext, *, start_ms: int, end_ms: int):
        if self._gateway is None:
            await self.collect(context)
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
            credentials = replace(credentials, proxy_url=SecretStr(f"{context.instance.proxy.type.value}://{proxy_url}"))
        self._gateway = self._gateway_factory(credentials, self._request_timeout_ms)
        self._history_source = WeexLiveTradeHistorySource(self._gateway)

    def _read_snapshot(self):
        assert self._gateway is not None
        return self._gateway.account_balance_rows("live"), self._gateway.all_position_rows("live")

    async def _collect_history(self, context: AccountTelemetryContext, now_ms: int):
        try:
            return await self._sync_history(context, now_ms), None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = type(exc).__name__
            self._ledger.refresh_sessions(context.instance.id, context.instance.mode.value, now_ms=now_ms, source_complete=False, stale=True)
            return None, failure

    def _activity_log(self, result, failure: str | None) -> str | None:
        if failure is not None:
            changed = self._last_history_failure_type != failure
            self._last_history_failure_type = failure
            return f"成交历史同步待核验：{failure}；钱包与仓位数据仍已更新" if changed else None
        if self._last_history_failure_type is not None:
            self._last_history_failure_type = None
            return "成交历史同步已恢复"
        if result is not None and result.fills_inserted:
            return f"只读成交历史已同步 {result.fills_inserted} 笔；历史完整：{'是' if result.aggregate.complete else '否'}"
        return None

    def _phase(self, failure: str | None) -> str:
        if failure is not None:
            return f"WEEX 钱包与仓位已同步；成交历史待核验 ({failure})"
        if self._history_scan_active:
            return "WEEX 只读遥测已同步 / 历史扫描中"
        return self._history_coverage_reason or "WEEX 只读遥测已同步"

    async def _sync_history(self, context: AccountTelemetryContext, now_ms: int):
        assert self._history_source is not None
        if not self._history_scan_active:
            self._begin_history_scan(context, now_ms)
        result = await self._history_synchronizer.sync(
            context.instance.id,
            TradeHistoryContext(context.instance, context.credentials),
            self._history_source,
            today_start_ms=shanghai_day_start_ms(now_ms),
            cursor=self._history_cursor,
            coverage_start_ms=self._history_scan_start_ms,
        )
        self._history_cursor = result.next_cursor
        if result.next_cursor is None:
            self._history_scan_active = False
            self._last_history_end_ms = self._history_scan_end_ms
            self._history_scan_end_ms = None
            self._history_scan_start_ms = None
        return result

    def _begin_history_scan(self, context: AccountTelemetryContext, now_ms: int) -> None:
        checkpoint = self._ledger.sync_checkpoint(context.instance.id, context.instance.mode.value) if self._last_history_end_ms is None else None
        if self._last_history_end_ms is not None:
            start_ms = max(0, self._last_history_end_ms - HISTORY_OVERLAP_MS)
            complete = self._history_coverage_complete
        elif isinstance(checkpoint.get("high_watermark_ms") if checkpoint else None, int) and checkpoint["high_watermark_ms"] > 0:
            start_ms = max(0, checkpoint["high_watermark_ms"] - HISTORY_OVERLAP_MS)
            complete = bool(checkpoint.get("coverage_complete"))
            self._history_coverage_reason = None if complete else "WEEX 只读遥测已同步 / 历史待核验"
        else:
            retention_start = max(0, now_ms - self._history_lookback_ms + 1)
            configured_start = context.instance.history_start_at_ms
            start_ms = max(retention_start, configured_start) if configured_start is not None else retention_start
            complete = configured_start is not None and configured_start >= retention_start
            self._history_coverage_reason = None if complete else (
                "WEEX 只读遥测已同步 / 历史起点超出可用窗口" if configured_start is not None else None
            )
        self._history_coverage_complete = complete
        self._history_source.begin(start_ms, now_ms, coverage_complete=complete)
        self._history_cursor = None
        self._history_scan_active = True
        self._history_scan_start_ms = start_ms
        self._history_scan_end_ms = now_ms


class WeexReadonlyAccountTelemetryAdapterFactory:
    def __init__(self, ledger: TradeVolumeLedger, *, request_timeout_ms: int = 15_000, history_lookback_days: int = 365, history_pages_per_poll: int = 1, gateway_factory: GatewayFactory | None = None) -> None:
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

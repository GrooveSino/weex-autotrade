from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from pydantic import SecretStr
from weex_cli.config import Credentials, Settings
from weex_cli.gateway import WeexGateway

from .models import ExposureSnapshot, ProxyStatus, TradingMode, VolumeSnapshot, WalletSnapshot
from .telemetry import AccountTelemetry, AccountTelemetryAdapter, AccountTelemetryContext
from .vault import CredentialMaterial
from .volume_history import (
    InMemoryTradeVolumeLedger,
    NormalizedTradeFill,
    TradeHistoryContext,
    TradeHistoryPage,
    TradeHistorySource,
    TradeHistorySynchronizer,
    TradeVolumeLedger,
    shanghai_day_start_ms,
)

DAY_MS = 24 * 60 * 60 * 1000
MAX_HISTORY_WINDOW_MS = 7 * DAY_MS
HISTORY_OVERLAP_MS = 5 * 60 * 1000


class WeexReadonlyError(RuntimeError):
    pass


class MissingAccountCredentials(WeexReadonlyError):
    pass


class ReadonlyLiveAccountRequired(WeexReadonlyError):
    pass


class InvalidWeexPayload(WeexReadonlyError):
    pass


class ReadonlyWeexGateway(Protocol):
    def account_balance_rows(self, mode: str) -> list[dict[str, Any]]: ...

    def all_position_rows(self, mode: str) -> list[dict[str, Any]]: ...

    def trade_rows(
        self,
        mode: str,
        symbol: str | None,
        *,
        start_time: int,
        end_time: int,
        limit: int,
        page: int | None = None,
    ) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


GatewayFactory = Callable[[CredentialMaterial, int], ReadonlyWeexGateway]


def build_readonly_gateway(material: CredentialMaterial, timeout_ms: int) -> ReadonlyWeexGateway:
    settings = Settings(
        credentials=Credentials(
            api_key=material.api_key.get_secret_value(),
            api_secret=material.api_secret.get_secret_value(),
            passphrase=material.passphrase.get_secret_value(),
        ),
        default_mode="live",
        live_trading_enabled=False,
        timeout_ms=timeout_ms,
        enable_rate_limit=True,
    )
    return WeexGateway(settings, proxy_url=material.proxy_url.get_secret_value())


class WeexLiveTradeHistorySource(TradeHistorySource):
    """Stateful, account-local scanner for WEEX's cursorless seven-day windows."""

    def __init__(self, gateway: ReadonlyWeexGateway) -> None:
        self._gateway = gateway
        self._pending: list[tuple[int, int]] = []
        self._expected_cursor: str | None = None
        self._scan_id = 0
        self._page_sequence = 0
        self._coverage_complete = False
        self._truncated = False
        self._active = False

    def begin(self, start_ms: int, end_ms: int, *, coverage_complete: bool) -> None:
        start_ms = max(0, min(start_ms, end_ms))
        windows: list[tuple[int, int]] = []
        cursor = start_ms
        while cursor <= end_ms:
            window_end = min(end_ms, cursor + MAX_HISTORY_WINDOW_MS - 1)
            windows.append((cursor, window_end))
            cursor = window_end + 1
        # pop() processes oldest windows first, so a persisted high-watermark
        # always represents contiguous coverage and is safe to resume.
        self._pending = list(reversed(windows))
        self._expected_cursor = None
        self._scan_id += 1
        self._page_sequence = 0
        self._coverage_complete = coverage_complete
        self._truncated = False
        self._active = True

    async def fetch_page(
        self,
        context: TradeHistoryContext,
        *,
        cursor: str | None,
        limit: int,
    ) -> TradeHistoryPage:
        if not self._active or cursor != self._expected_cursor:
            raise InvalidWeexPayload("history cursor does not match the active account scan")
        if context.instance.mode is not TradingMode.LIVE:
            raise ReadonlyLiveAccountRequired("WEEX read-only history currently supports Live accounts")

        start_ms, end_ms = self._pending.pop()
        rows = await asyncio.to_thread(
            self._gateway.trade_rows,
            "live",
            None,
            start_time=start_ms,
            end_time=end_ms,
            limit=limit,
        )
        if not isinstance(rows, list):
            raise InvalidWeexPayload("WEEX trade history returned a non-list response")

        fills: tuple[NormalizedTradeFill, ...] = ()
        high_watermark_ms: int | None = None
        if len(rows) >= limit and start_ms < end_ms:
            midpoint = (start_ms + end_ms) // 2
            self._pending.extend(((midpoint + 1, end_ms), (start_ms, midpoint)))
        else:
            fills = tuple(
                fill
                for row in rows
                if isinstance(row, dict)
                if (fill := _normalize_live_fill(row, start_ms, end_ms)) is not None
            )
            if len(rows) >= limit:
                self._truncated = True
            high_watermark_ms = end_ms

        self._page_sequence += 1
        if self._pending:
            next_cursor = f"scan-{self._scan_id}-{self._page_sequence}"
            self._expected_cursor = next_cursor
        else:
            next_cursor = None
            self._expected_cursor = None
            self._active = False
        return TradeHistoryPage(
            fills=fills,
            next_cursor=next_cursor,
            complete=self._coverage_complete and not self._truncated,
            high_watermark_ms=high_watermark_ms,
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
        self._history_synchronizer = TradeHistorySynchronizer(
            ledger,
            page_size=100,
            max_pages=history_pages_per_poll,
        )
        self._gateway: ReadonlyWeexGateway | None = None
        self._history_source: WeexLiveTradeHistorySource | None = None
        self._history_cursor: str | None = None
        self._history_scan_active = False
        self._history_scan_end_ms: int | None = None
        self._last_history_end_ms: int | None = None
        self._history_coverage_complete = False
        self._history_coverage_reason: str | None = None

    async def collect(self, context: AccountTelemetryContext) -> AccountTelemetry:
        if context.instance.mode is not TradingMode.LIVE:
            raise ReadonlyLiveAccountRequired("WEEX read-only telemetry currently supports Live accounts")
        if context.credentials is None:
            raise MissingAccountCredentials("account credentials are unavailable")
        if self._gateway is None:
            credentials = context.credentials
            proxy_url = credentials.proxy_url.get_secret_value()
            if "://" not in proxy_url:
                scheme = "socks5" if context.instance.proxy.type.value == "socks5" else "https"
                credentials = replace(credentials, proxy_url=SecretStr(f"{scheme}://{proxy_url}"))
            self._gateway = self._gateway_factory(credentials, self._request_timeout_ms)
            self._history_source = WeexLiveTradeHistorySource(self._gateway)

        started = time.perf_counter()
        balance_rows, position_rows = await asyncio.to_thread(self._read_snapshot)
        wallet = _wallet_snapshot(balance_rows)
        exposure = _exposure_snapshot(position_rows)
        now_ms = time.time_ns() // 1_000_000
        history_result = await self._sync_history(context, now_ms)
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        history_scanning = self._history_scan_active
        activity_log = None
        if history_result.fills_inserted:
            activity_log = (
                f"只读成交历史已同步 {history_result.fills_inserted} 笔；"
                f"历史完整：{'是' if history_result.aggregate.complete else '否'}"
            )

        return AccountTelemetry(
            wallet=wallet,
            volume=VolumeSnapshot(
                lifetime=_finite_float(history_result.aggregate.lifetime, "lifetime volume"),
                today=_finite_float(history_result.aggregate.today, "today volume"),
                complete=history_result.aggregate.complete,
                session=self._ledger.latest_session(context.instance.id, context.instance.mode.value),
            ),
            exposure=exposure,
            cycle_completed=context.instance.cycle.completed,
            proxy_status=ProxyStatus.HEALTHY,
            proxy_latency_ms=latency_ms,
            proxy_location="WEEX / account-bound",
            phase=(
                "WEEX 只读遥测已同步 / 历史扫描中"
                if history_scanning
                else self._history_coverage_reason or "WEEX 只读遥测已同步"
            ),
            activity_log=activity_log,
        )

    async def aclose(self) -> None:
        gateway = self._gateway
        self._gateway = None
        self._history_source = None
        if gateway is not None:
            await asyncio.to_thread(gateway.close)

    async def authoritative_fills(
        self,
        context: AccountTelemetryContext,
        *,
        start_ms: int,
        end_ms: int,
    ) -> tuple[tuple[NormalizedTradeFill, ...], bool, str]:
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
        )
        fills = temporary.fills_for_account(context.instance.id, context.instance.mode.value, start_ms)
        complete = result.stop_reason == "history_exhausted" and result.aggregate.complete
        return fills, complete, result.stop_reason

    def _read_snapshot(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        assert self._gateway is not None
        return self._gateway.account_balance_rows("live"), self._gateway.all_position_rows("live")

    async def _sync_history(self, context: AccountTelemetryContext, now_ms: int):
        assert self._history_source is not None
        if not self._history_scan_active:
            if self._last_history_end_ms is None:
                checkpoint = self._ledger.sync_checkpoint(context.instance.id, context.instance.mode.value)
                persisted_high_watermark = checkpoint.get("high_watermark_ms") if checkpoint else None
                if isinstance(persisted_high_watermark, int) and persisted_high_watermark > 0:
                    start_ms = max(0, persisted_high_watermark - HISTORY_OVERLAP_MS)
                    coverage_complete = bool(checkpoint and checkpoint.get("coverage_complete"))
                    self._history_coverage_complete = coverage_complete
                    self._history_coverage_reason = None if coverage_complete else "WEEX 只读遥测已同步 / 历史待核验"
                else:
                    retention_start_ms = max(0, now_ms - self._history_lookback_ms + 1)
                    configured_start_ms = context.instance.history_start_at_ms
                    start_ms = (
                        max(retention_start_ms, configured_start_ms)
                        if configured_start_ms is not None
                        else retention_start_ms
                    )
                    coverage_complete = configured_start_ms is not None and configured_start_ms >= retention_start_ms
                    self._history_coverage_complete = coverage_complete
                    self._history_coverage_reason = (
                        None
                        if coverage_complete
                        else "WEEX 只读遥测已同步 / 历史起点超出可用窗口"
                        if configured_start_ms is not None
                        else None
                    )
            else:
                start_ms = max(0, self._last_history_end_ms - HISTORY_OVERLAP_MS)
                coverage_complete = self._history_coverage_complete
            self._history_source.begin(start_ms, now_ms, coverage_complete=coverage_complete)
            self._history_cursor = None
            self._history_scan_active = True
            self._history_scan_end_ms = now_ms

        result = await self._history_synchronizer.sync(
            context.instance.id,
            TradeHistoryContext(context.instance, context.credentials),
            self._history_source,
            today_start_ms=shanghai_day_start_ms(now_ms),
            cursor=self._history_cursor,
        )
        self._history_cursor = result.next_cursor
        if result.next_cursor is None:
            self._history_scan_active = False
            self._last_history_end_ms = self._history_scan_end_ms
            self._history_scan_end_ms = None
        return result


class WeexReadonlyAccountTelemetryAdapterFactory:
    def __init__(
        self,
        ledger: TradeVolumeLedger,
        *,
        request_timeout_ms: int = 5_000,
        history_lookback_days: int = 365,
        history_pages_per_poll: int = 1,
        gateway_factory: GatewayFactory = build_readonly_gateway,
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


def _wallet_snapshot(rows: list[dict[str, Any]]) -> WalletSnapshot:
    row = next((item for item in rows if str(item.get("asset") or "").upper() == "USDT"), None)
    if row is None:
        raise InvalidWeexPayload("WEEX balance response has no USDT row")
    balance = _decimal(row.get("balance"), "balance")
    available = _decimal(row.get("availableBalance"), "available balance")
    unrealized = _decimal(row.get("unrealizePnl"), "unrealized PnL")
    return WalletSnapshot(
        equity=_finite_float(balance + unrealized, "equity"),
        available=_finite_float(available, "available balance"),
        unrealized_pnl=_finite_float(unrealized, "unrealized PnL"),
    )


def _exposure_snapshot(rows: list[dict[str, Any]]) -> ExposureSnapshot:
    btc_long = Decimal(0)
    eth_short = Decimal(0)
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        side = str(row.get("side") or "").upper()
        open_value = abs(_decimal(row.get("openValue"), "position open value"))
        if symbol.startswith("BTC") and side == "LONG":
            btc_long += open_value
        elif symbol.startswith("ETH") and side == "SHORT":
            eth_short += open_value
    return ExposureSnapshot(
        btc_long=_finite_float(btc_long, "BTC long exposure"),
        eth_short=_finite_float(eth_short, "ETH short exposure"),
    )


def _normalize_live_fill(row: dict[str, Any], start_ms: int, end_ms: int) -> NormalizedTradeFill | None:
    try:
        executed_at_ms = int(row.get("time"))
    except (TypeError, ValueError):
        return None
    if not start_ms <= executed_at_ms <= end_ms:
        return None
    symbol = str(row.get("symbol") or "").upper()
    if not symbol:
        return None
    # quoteQty is the only authoritative turnover source. Price * quantity is
    # intentionally not used: planned/order/position estimates must never enter
    # the local ledger.
    raw_quote = row.get("quoteQty")
    if raw_quote is None or raw_quote == "":
        return None
    quote_volume = _decimal(raw_quote, "fill quote quantity")
    if quote_volume <= 0:
        return None
    trade_id = str(row.get("id") or "").strip()
    if not trade_id:
        trade_id = ":".join(
            (
                symbol,
                str(row.get("orderId") or ""),
                str(executed_at_ms),
                str(row.get("price") or ""),
                str(row.get("qty") or ""),
            )
        )
    return NormalizedTradeFill(
        identity=trade_id,
        executed_at_ms=executed_at_ms,
        quote_volume=quote_volume,
        symbol=symbol,
        order_id=str(row.get("orderId") or ""),
        base_quantity=_decimal(row.get("qty"), "fill quantity"),
        side=str(row.get("side") or "").upper(),
        position_side=str(row.get("positionSide") or "").upper(),
        position_action=(
            "close"
            if str(row.get("positionAction") or row.get("tradeSide") or "").lower()
            in {"close", "close_short", "close_long"}
            else "open"
            if str(row.get("positionAction") or row.get("tradeSide") or "").lower()
            in {"open", "open_short", "open_long"}
            else "unknown"
        ),
        maker=(None if row.get("maker") is None else bool(row.get("maker"))),
        commission=_decimal(row.get("commission"), "commission"),
        commission_asset=str(row.get("commissionAsset") or "") or None,
        realized_pnl=_decimal(row.get("realizedPnl"), "realized pnl"),
        source="weex_user_trades",
        authoritative=True,
    )


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value if value is not None and value != "" else "0"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidWeexPayload(f"WEEX {field} is not numeric") from exc
    if not result.is_finite():
        raise InvalidWeexPayload(f"WEEX {field} is not finite")
    return result


def _finite_float(value: Decimal, field: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise InvalidWeexPayload(f"WEEX {field} is outside the supported range")
    return result

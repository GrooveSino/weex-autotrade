"""User-triggered WEEX turnover reports backed by the authoritative fill ledger."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from decimal import Decimal

from fleet_api.auth.vault import CredentialMaterial
from fleet_api.models import (
    AccountInstance,
    AccountTradeVolumePeriod,
    AccountTradeVolumeProjection,
    AccountTradeVolumeReportResponse,
    TradingMode,
)
from fleet_api.volume.core.volume_history import (
    FillConflictError,
    NormalizedTradeFill,
    TradeVolumeAggregate,
    TradeVolumeLedger,
    shanghai_day_start_ms,
)

DAY_MS = 24 * 60 * 60 * 1_000
SUPPORTED_LOOKBACK_DAYS = frozenset({1, 7, 30})
AuthoritativeFillReader = Callable[
    [str, int, int],
    Awaitable[tuple[tuple[NormalizedTradeFill, ...], bool, str]],
]
VolumeProjectionUpdater = Callable[[str, TradeVolumeAggregate], object]


class AccountTradeVolumeReportError(RuntimeError):
    """A safe, actionable report error suitable for the public API."""

    def __init__(self, code: str, message: str, action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.action = action


class AccountTradeVolumeReportService:
    def __init__(
        self,
        authoritative_fill_reader: AuthoritativeFillReader,
        ledger: TradeVolumeLedger,
        projection_updater: VolumeProjectionUpdater,
        *,
        max_concurrent_reports: int = 1,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if max_concurrent_reports < 1:
            raise ValueError("max_concurrent_reports must be positive")
        self._authoritative_fill_reader = authoritative_fill_reader
        self._ledger = ledger
        self._projection_updater = projection_updater
        self._report_slots = asyncio.Semaphore(max_concurrent_reports)
        self._account_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    async def report(
        self,
        instance: AccountInstance,
        material: CredentialMaterial | None,
        lookback_days: Iterable[int],
    ) -> AccountTradeVolumeReportResponse:
        _validate_account(instance, material)
        periods = _normalise_lookbacks(lookback_days)
        account_lock = await self._lock_for(instance.id)
        async with account_lock, self._report_slots:
            return await self._read_record_and_project(instance, periods)

    async def _lock_for(self, account_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._account_locks.setdefault(account_id, asyncio.Lock())

    async def _read_record_and_project(
        self,
        instance: AccountInstance,
        periods: tuple[int, ...],
    ) -> AccountTradeVolumeReportResponse:
        end_at_ms = self._clock_ms()
        scan_start_ms = max(0, end_at_ms - max(periods) * DAY_MS)
        try:
            fills, window_complete, stop_reason = await self._authoritative_fill_reader(
                instance.id,
                scan_start_ms,
                end_at_ms,
            )
            verified_fills = _verified_unique_fills(fills, scan_start_ms, end_at_ms)
        except AccountTradeVolumeReportError:
            raise
        except Exception as exc:
            raise AccountTradeVolumeReportError(
                "trade_history_unavailable",
                "交易所近期成交历史暂时无法读取，未修改账号、策略、仓位或挂单。",
                "检查该账号代理和 API 只读权限后，等待片刻再点击统计。",
            ) from exc
        try:
            inserted = self._ledger.record_account_fills(instance.id, instance.mode.value, verified_fills)
            aggregate = self._ledger.aggregate(instance.id, shanghai_day_start_ms(end_at_ms))
        except FillConflictError as exc:
            raise AccountTradeVolumeReportError(
                "trade_history_conflict",
                "成交历史与本地同一笔成交的已核验数据不一致，因此没有重复累计。",
                "请稍后重新统计；如果仍然出现，请联系管理员按错误时间检查成交账本。",
            ) from exc
        except Exception as exc:
            raise AccountTradeVolumeReportError(
                "trade_ledger_write_failed",
                "已核验成交未能完整写入累计交易量账本，未修改策略、仓位或挂单。",
                "等待片刻后重新统计；账本会按成交身份自动去重，不会重复累计。",
            ) from exc
        try:
            self._projection_updater(instance.id, aggregate)
        except Exception as exc:
            raise AccountTradeVolumeReportError(
                "volume_projection_refresh_failed",
                "成交已去重写入累计账本，但账号页面暂时没有刷新。",
                "刷新账号列表；如果累计值仍未更新，再次统计同一周期即可重新投影且不会重复累计。",
            ) from exc
        complete = window_complete and stop_reason == "history_exhausted"
        return AccountTradeVolumeReportResponse(
            periods=tuple(_period_from_fills(verified_fills, days, end_at_ms, complete) for days in periods),
            generated_at_ms=end_at_ms,
            ledger_scanned_fill_count=len(verified_fills),
            ledger_inserted_fill_count=inserted,
            ledger_deduplicated_fill_count=len(verified_fills) - inserted,
            ledger_lifetime_quote_volume=aggregate.lifetime,
            ledger_today_quote_volume=aggregate.today,
            ledger_source_complete=aggregate.complete,
            account_volume=AccountTradeVolumeProjection(
                lifetime_quote_volume=aggregate.lifetime,
                today_quote_volume=aggregate.today,
                source_complete=aggregate.complete,
            ),
        )


def _validate_account(instance: AccountInstance, material: CredentialMaterial | None) -> None:
    if instance.mode is not TradingMode.LIVE:
        raise AccountTradeVolumeReportError(
            "live_account_required",
            "近期交易量统计当前仅支持实盘账号。",
            "请选择已绑定 API 凭据的实盘账号后重试。",
        )
    if material is None:
        raise AccountTradeVolumeReportError(
            "credentials_missing",
            "该账号缺少可用的只读 API 凭据，无法读取交易历史。",
            "在“编辑账号与代理”中完整保存 API Key、Secret 和 Passphrase 后重试。",
        )


def _normalise_lookbacks(values: Iterable[int]) -> tuple[int, ...]:
    periods = tuple(sorted(set(values)))
    if not periods or any(days not in SUPPORTED_LOOKBACK_DAYS for days in periods):
        raise AccountTradeVolumeReportError(
            "invalid_lookback",
            "统计周期仅支持近 1 天、近 7 天或近 30 天。",
            "选择页面提供的统计按钮后重试。",
        )
    return periods


def _verified_unique_fills(
    fills: tuple[NormalizedTradeFill, ...],
    start_at_ms: int,
    end_at_ms: int,
) -> tuple[NormalizedTradeFill, ...]:
    unique: dict[str, NormalizedTradeFill] = {}
    for fill in fills:
        if not isinstance(fill, NormalizedTradeFill):
            raise AccountTradeVolumeReportError(
                "trade_history_invalid",
                "交易所返回的成交记录格式无法核验。",
                "等待片刻后重新统计；如果持续发生，请检查账号 API 权限。",
            )
        if not fill.authoritative or not start_at_ms <= fill.executed_at_ms <= end_at_ms:
            continue
        if not fill.quote_volume.is_finite() or fill.quote_volume <= 0:
            continue
        existing = unique.get(fill.identity)
        if existing is not None and existing != fill:
            raise FillConflictError(f"fill identity {fill.identity!r} changed within report")
        unique[fill.identity] = fill
    return tuple(sorted(unique.values(), key=lambda item: (item.executed_at_ms, item.identity)))


def _period_from_fills(
    fills: tuple[NormalizedTradeFill, ...],
    lookback_days: int,
    end_at_ms: int,
    complete: bool,
) -> AccountTradeVolumePeriod:
    start_at_ms = max(0, end_at_ms - lookback_days * DAY_MS)
    selected = tuple(fill for fill in fills if fill.executed_at_ms >= start_at_ms)
    return AccountTradeVolumePeriod(
        lookback_days=lookback_days,
        start_at_ms=start_at_ms,
        end_at_ms=end_at_ms,
        total_quote_volume=_sum_quote(selected),
        maker_quote_volume=_sum_quote(tuple(fill for fill in selected if fill.maker is True)),
        taker_quote_volume=_sum_quote(tuple(fill for fill in selected if fill.maker is False)),
        unknown_liquidity_quote_volume=_sum_quote(tuple(fill for fill in selected if fill.maker is None)),
        trade_count=len(selected),
        complete=complete,
        warnings=[] if complete else ["部分时间窗口无法完整确认，统计金额可能低于实际值；请稍后重新统计。"],
    )


def _sum_quote(fills: tuple[NormalizedTradeFill, ...]) -> Decimal:
    return sum((fill.quote_volume for fill in fills), start=Decimal(0))

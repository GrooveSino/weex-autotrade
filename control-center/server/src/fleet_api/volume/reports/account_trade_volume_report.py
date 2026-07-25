"""Account-scoped, on-demand WEEX turnover reports.

This is deliberately separate from the persistent fill ledger and its quiet
background synchronizer. A user presses this report button; no strategy,
checkpoint, telemetry state, or order command is changed as a consequence.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from contextlib import suppress
from decimal import Decimal, InvalidOperation

from weex_cli.trade_reporting import TradeReportService

from fleet_api.auth.vault import CredentialMaterial
from fleet_api.market.weex_readonly import GatewayFactory, ReadonlyWeexGateway
from fleet_api.models import AccountInstance, AccountTradeVolumePeriod, AccountTradeVolumeReportResponse, TradingMode

DAY_MS = 24 * 60 * 60 * 1_000
SUPPORTED_LOOKBACK_DAYS = frozenset({1, 7, 30})


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
        gateway_factory: GatewayFactory,
        *,
        request_timeout_ms: int,
        max_concurrent_reports: int = 1,
    ) -> None:
        if request_timeout_ms < 1_000:
            raise ValueError("request_timeout_ms must be at least 1000")
        if max_concurrent_reports < 1:
            raise ValueError("max_concurrent_reports must be positive")
        self._gateway_factory = gateway_factory
        self._request_timeout_ms = request_timeout_ms
        self._report_slots = asyncio.Semaphore(max_concurrent_reports)
        self._account_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def report(
        self,
        instance: AccountInstance,
        material: CredentialMaterial | None,
        lookback_days: Iterable[int],
    ) -> AccountTradeVolumeReportResponse:
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
        periods = _normalise_lookbacks(lookback_days)
        account_lock = await self._lock_for(instance.id)
        async with account_lock, self._report_slots:
            return await asyncio.to_thread(self._report_sync, material, periods)

    async def _lock_for(self, account_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._account_locks.setdefault(account_id, asyncio.Lock())

    def _report_sync(
        self,
        material: CredentialMaterial,
        periods: tuple[int, ...],
    ) -> AccountTradeVolumeReportResponse:
        gateway: ReadonlyWeexGateway | None = None
        try:
            gateway = self._gateway_factory(material, self._request_timeout_ms)
            reporter = TradeReportService(gateway)  # type: ignore[arg-type]
            end_at_ms = time.time_ns() // 1_000_000
            results = tuple(
                _period_from_report(
                    reporter.report(
                        mode="live",
                        symbol=None,
                        start_time=end_at_ms - days * DAY_MS,
                        end_time=end_at_ms,
                    ),
                    days,
                )
                for days in periods
            )
            return AccountTradeVolumeReportResponse(periods=results, generated_at_ms=end_at_ms)
        except AccountTradeVolumeReportError:
            raise
        except Exception as exc:
            raise AccountTradeVolumeReportError(
                "trade_history_unavailable",
                "交易所近期成交历史暂时无法读取，未修改账号、策略或仓位。",
                "检查该账号代理和 API 只读权限后，等待片刻再点击统计。",
            ) from exc
        finally:
            if gateway is not None:
                with suppress(Exception):
                    gateway.close()


def _normalise_lookbacks(values: Iterable[int]) -> tuple[int, ...]:
    periods = tuple(sorted(set(values)))
    if not periods or any(days not in SUPPORTED_LOOKBACK_DAYS for days in periods):
        raise AccountTradeVolumeReportError(
            "invalid_lookback",
            "统计周期仅支持近 1 天、近 7 天或近 30 天。",
            "选择页面提供的统计按钮后重试。",
        )
    return periods


def _period_from_report(report: object, lookback_days: int) -> AccountTradeVolumePeriod:
    if not isinstance(report, dict):
        raise AccountTradeVolumeReportError(
            "trade_history_invalid",
            "交易所返回的成交历史格式无法核验。",
            "等待片刻后重新统计；如果持续发生，请检查账号 API 权限。",
        )
    summary = report.get("summary")
    if not isinstance(summary, dict):
        raise AccountTradeVolumeReportError(
            "trade_history_invalid",
            "交易所返回的成交汇总格式无法核验。",
            "等待片刻后重新统计；如果持续发生，请检查账号 API 权限。",
        )
    return AccountTradeVolumePeriod(
        lookback_days=lookback_days,
        start_at_ms=_positive_int(report.get("start_time")),
        end_at_ms=_positive_int(report.get("end_time")),
        total_quote_volume=_decimal(summary.get("total_quote_volume")),
        maker_quote_volume=_decimal(summary.get("maker_quote_volume")),
        taker_quote_volume=_decimal(summary.get("taker_quote_volume")),
        unknown_liquidity_quote_volume=_decimal(summary.get("unknown_liquidity_quote_volume")),
        trade_count=_non_negative_int(summary.get("trade_count")),
        complete=bool(report.get("complete")),
        warnings=_safe_warnings(report.get("warnings")),
    )


def _decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AccountTradeVolumeReportError(
            "trade_history_invalid",
            "交易所返回的成交金额无法核验。",
            "等待片刻后重新统计；如果持续发生，请检查账号 API 权限。",
        ) from exc
    if not parsed.is_finite() or parsed < 0:
        raise AccountTradeVolumeReportError(
            "trade_history_invalid",
            "交易所返回的成交金额无法核验。",
            "等待片刻后重新统计；如果持续发生，请检查账号 API 权限。",
        )
    return parsed


def _positive_int(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _non_negative_int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _safe_warnings(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        return []
    return ["部分时间窗口无法完整确认，统计金额可能低于实际值；请稍后重新统计。"]

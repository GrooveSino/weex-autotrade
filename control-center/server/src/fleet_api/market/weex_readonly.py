from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from weex_cli.control_api.exchange import Credentials, Settings, WeexGateway

from fleet_api.auth.vault import CredentialMaterial
from fleet_api.models import ExposureSnapshot, TradingMode, WalletSnapshot
from fleet_api.volume.core.volume_history import (
    NormalizedTradeFill,
    TradeHistoryContext,
    TradeHistoryPage,
    TradeHistorySource,
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
    return WeexGateway(
        settings,
        proxy_url=material.proxy_url.get_secret_value() if material.proxy_url is not None else None,
    )


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
        self._scan_start_ms: int | None = None
        self._scan_end_ms: int | None = None

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
        self._scan_start_ms = start_ms
        self._scan_end_ms = end_ms

    def snapshot(self) -> dict[str, object]:
        """Return only scheduler state; no gateway response data is retained."""
        return {
            "pending_windows": [[start, end] for start, end in self._pending],
            "expected_cursor": self._expected_cursor,
            "scan_id": self._scan_id,
            "page_sequence": self._page_sequence,
            "coverage_complete": self._coverage_complete,
            "truncated": self._truncated,
            "active": self._active,
            "scan_start_ms": self._scan_start_ms,
            "scan_end_ms": self._scan_end_ms,
        }

    def restore(self, state: Mapping[str, object]) -> bool:
        """Restore a checkpointed window queue after an executor restart."""
        raw_windows = state.get("pending_windows")
        if not isinstance(raw_windows, list):
            return False
        pending: list[tuple[int, int]] = []
        for value in raw_windows:
            if not isinstance(value, list) or len(value) != 2:
                return False
            start, end = value
            if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
                return False
            pending.append((start, end))
        expected_cursor = state.get("expected_cursor")
        if expected_cursor is not None and not isinstance(expected_cursor, str):
            return False
        self._pending = pending
        self._expected_cursor = expected_cursor
        self._scan_id = _non_negative_int(state.get("scan_id"))
        self._page_sequence = _non_negative_int(state.get("page_sequence"))
        self._coverage_complete = bool(state.get("coverage_complete"))
        self._truncated = bool(state.get("truncated"))
        self._active = bool(state.get("active")) and bool(pending)
        self._scan_start_ms = _optional_non_negative_int(state.get("scan_start_ms"))
        self._scan_end_ms = _optional_non_negative_int(state.get("scan_end_ms"))
        return self._active

    @property
    def scan_start_ms(self) -> int | None:
        return self._scan_start_ms

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

        # Keep the window in the durable source snapshot until the request has
        # succeeded. A timeout must retry the same coverage instead of silently
        # dropping the failed interval from the baseline.
        start_ms, end_ms = self._pending[-1]
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
        self._pending.pop()

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
            window_complete=not self._truncated,
        )


def _non_negative_int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _optional_non_negative_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


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

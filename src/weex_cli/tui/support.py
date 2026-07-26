"""Validation, timeline labels, and redacted metrics for the TUI."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from weex_cli.core.errors import SafetyError, ValidationError, WeexCliError
from weex_cli.core.redaction import redact_text


def _positive_decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{name} 必须是数字") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValidationError(f"{name} 必须大于 0")
    return parsed


def _nonnegative_decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{name} 必须是数字") from None
    if not parsed.is_finite() or parsed < 0:
        raise ValidationError(f"{name} 不能小于 0")
    return parsed


def _decimal_or_zero(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return parsed if parsed.is_finite() else Decimal(0)


def _phase_name(event: str, payload: Mapping[str, Any]) -> str:
    if event == "tui_campaign_console_opened":
        return "任务控制台已打开"
    if event == "tui_worker_started":
        return "后台 worker 已启动"
    if event == "tui_execution_validation_started":
        return "执行授权校验"
    if event == "tui_execution_validation_completed":
        return "执行授权校验完成"
    if event == "tui_execution_preflight_started":
        return "实盘账户预检"
    if event == "tui_execution_preflight_completed":
        return "实盘账户预检完成"
    if event == "tui_execution_failed":
        return f"{payload.get('stage', 'worker')} 失败"
    if event == "tui_execution_result_received":
        return "收到 Campaign 结果"
    if event == "tui_worker_finished":
        return "后台 worker 已结束"
    if event.startswith("campaign_child_planning") or event == "cycle_preparing":
        return "轮次规划"
    if event in {"hold_started", "hold_completed"}:
        return "持仓等待"
    if event in {"round_gap_started", "round_gap_completed"}:
        return "轮次间隔"
    if event.startswith("campaign_boundary") or event.startswith("final_acceptance"):
        return "账户边界核对"
    if event.startswith("leg"):
        action = "开仓" if payload.get("action") == "open" else "平仓"
        return f"{payload.get('symbol', '')} {action}".strip()
    if event == "campaign_finished":
        return "完成"
    return event.replace("_", " ")


def _worker_state(event: str, payload: Mapping[str, Any]) -> str:
    if event == "tui_campaign_console_opened":
        return "已排队"
    if event in {"tui_worker_started", "tui_execution_validation_started", "tui_execution_preflight_started"}:
        return "正在启动"
    if event == "tui_execution_failed":
        return "启动失败" if payload.get("stage") != "campaign_execution" else "执行异常"
    if event in {"campaign_stopped", "stop_requested"}:
        return "安全停止中"
    if event in {"campaign_finished", "tui_worker_finished", "tui_execution_result_received"}:
        status = str(payload.get("status") or "")
        if status:
            return status
    return "运行中"


def _event_log_line(event: Mapping[str, Any]) -> str:
    try:
        timestamp_ms = int(event.get("timestamp_ms") or int(time.time() * 1000))
    except (TypeError, ValueError):
        timestamp_ms = int(time.time() * 1000)
    timestamp = time.strftime("%H:%M:%S", time.localtime(timestamp_ms / 1000))
    payload = {key: value for key, value in event.items() if key != "timestamp_ms"}
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return f"[{timestamp}] {rendered}"


def _result_metrics(result: Mapping[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "btc_quote": Decimal(0),
        "eth_quote": Decimal(0),
        "maker_count": 0,
        "taker_count": 0,
        "unknown_count": 0,
        "submissions": 0,
        "cancels": 0,
        "requotes": 0,
        "open_ms": 0,
        "close_ms": 0,
        "hold_seconds": 0.0,
        "gap_seconds": 0.0,
    }
    children = result.get("children")
    if isinstance(children, list):
        for child in children:
            if not isinstance(child, Mapping):
                continue
            cycles = child.get("cycles")
            if not isinstance(cycles, list):
                continue
            for cycle in cycles:
                if not isinstance(cycle, Mapping):
                    continue
                metrics["hold_seconds"] += float(cycle.get("hold_seconds") or 0)
                metrics["gap_seconds"] += float(cycle.get("round_gap_seconds") or 0)
                legs = cycle.get("legs")
                if not isinstance(legs, list):
                    continue
                for leg in legs:
                    if not isinstance(leg, Mapping):
                        continue
                    symbol = str(leg.get("symbol") or "").lower()
                    if symbol == "btc":
                        metrics["btc_quote"] += _decimal_or_zero(leg.get("quote_volume"))
                    elif symbol == "eth":
                        metrics["eth_quote"] += _decimal_or_zero(leg.get("quote_volume"))
                    metrics["maker_count"] += int(leg.get("maker_count") or 0)
                    metrics["taker_count"] += int(leg.get("taker_count") or 0)
                    metrics["unknown_count"] += int(leg.get("unknown_liquidity_count") or 0)
                    metrics["submissions"] += int(leg.get("submissions") or 0)
                    metrics["cancels"] += int(leg.get("cancels") or 0)
                    duration_key = "open_ms" if leg.get("action") == "open" else "close_ms"
                    metrics[duration_key] += int(leg.get("elapsed_ms") or 0)
    metrics["requotes"] = sum(
        1 for event in events if event.get("event") == "leg_progress" and event.get("progress_event") == "requote"
    )
    metrics["btc_quote"] = str(metrics["btc_quote"])
    metrics["eth_quote"] = str(metrics["eth_quote"])
    return metrics


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, SafetyError | ValidationError | WeexCliError):
        return redact_text(exc)
    return f"操作失败 ({type(exc).__name__})；未显示底层响应以保护账户凭据"

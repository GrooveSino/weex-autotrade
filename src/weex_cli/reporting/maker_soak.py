"""Renderer and writer for Demo Maker soak reports."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .report_support import decimal, escape, float_value, int_value, number, passed


def build_maker_soak_report(payload: Mapping[str, Any], *, generated_at: datetime | None = None) -> str:
    generated = (generated_at or datetime.now(UTC)).astimezone(UTC)
    rounds = [dict(row) for row in payload.get("rounds", []) if isinstance(row, Mapping)]
    requested = int_value(payload.get("rounds_requested"))
    completed = int_value(payload.get("rounds_completed"))
    lines = [
        "# WEEX Demo 纯 Maker 连续轮次报告",
        "",
        f"> 生成时间：{generated.strftime('%Y-%m-%d %H:%M:%S UTC')}。报告不包含账户、凭据或订单 ID。",
        "",
        "## 技术结论",
        "",
        (
            f"连续测试状态为 **{escape(payload.get('status'))}**（`{escape(payload.get('reason'))}`），"
            f"完成 {completed}/{requested} 轮，总成交量 {escape(payload.get('total_quote_volume'))} SUSDT，"
            f"总耗时 {float_value(payload.get('elapsed_seconds')):.3f} 秒。"
        ),
        "",
        "## 逐轮结果",
        "",
        (
            "| 轮次 | 状态 | 原因 | 成交量 SUSDT | 耗时 s | 提交 | 预检跳过 | 查询错误 | 撤单确认 | "
            "确认错误 | Post-Only 拒绝 | 最终仓位 | 活动订单 |"
        ),
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rounds:
        lines.append(
            "| {round} | {status} | {reason} | {volume} | {elapsed:.3f} | {submissions} | {preflight} | "
            "{observations} | {cancel_verifications} | {cancel_errors} | {rejections} | {position} | {orders} |".format(
                round=int_value(row.get("round")),
                status=escape(row.get("status")),
                reason=escape(row.get("reason")),
                volume=escape(row.get("total_quote_volume")),
                elapsed=float_value(row.get("elapsed_seconds")),
                submissions=int_value(row.get("submission_count")),
                preflight=int_value(row.get("preflight_skip_count")),
                observations=int_value(row.get("observation_error_count")),
                cancel_verifications=int_value(row.get("cancel_verification_attempt_count")),
                cancel_errors=int_value(row.get("cancel_verification_error_count")),
                rejections=int_value(row.get("post_only_rejection_count")),
                position=escape(row.get("final_position")),
                orders=escape(row.get("active_order_count")),
            )
        )
    lines.extend(
        [
            "",
            "## 逐腿审计",
            "",
            (
                "| 轮次 | 腿 | 动作 | 状态 | 耗时 ms | 提交 | 预检跳过 | 撤单 | 撤单确认 | "
                "确认错误 | Maker 成交事件 | 成交量 SUSDT | 终仓位 |"
            ),
            "|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in rounds:
        for leg in row.get("legs", []):
            if not isinstance(leg, Mapping):
                continue
            lines.append(
                "| {round} | {leg} | {action} | {status} | {elapsed} | {submissions} | {preflight} | "
                "{cancels} | {cancel_verifications} | {cancel_errors} | {fills} | {volume} | {position} |".format(
                    round=int_value(row.get("round")),
                    leg=int_value(leg.get("sequence")),
                    action=escape(leg.get("action")),
                    status=escape(leg.get("status")),
                    elapsed=int_value(leg.get("elapsed_ms")),
                    submissions=int_value(leg.get("submissions")),
                    preflight=int_value(leg.get("preflight_skips")),
                    cancels=int_value(leg.get("cancels")) + int_value(leg.get("venue_cancels")),
                    cancel_verifications=int_value(leg.get("cancel_verification_attempts")),
                    cancel_errors=int_value(leg.get("cancel_verification_errors")),
                    fills=int_value(leg.get("fill_count")),
                    volume=number(decimal(leg.get("quote_volume"))),
                    position=number(decimal(leg.get("final_position"))),
                )
            )
    all_flat = bool(rounds) and all(float_value(row.get("final_position")) == 0 for row in rounds)
    no_orders = bool(rounds) and all(int_value(row.get("active_order_count")) == 0 for row in rounds)
    lines.extend(
        [
            "",
            "## 验收检查",
            "",
            "| 检查项 | 结果 |",
            "|---|---|",
            f"| 完成全部 {requested} 轮 | {passed(completed == requested)} |",
            f"| 每轮最终空仓 | {passed(all_flat)} |",
            f"| 每轮最终无活动订单 | {passed(no_orders)} |",
            f"| Post-Only 拒绝为 0 | {passed(int_value(payload.get('total_post_only_rejections')) == 0)} |",
            "",
            "连续测试在任一轮失败后立即停止，不会自动清仓后继续下一轮。开仓和平仓成交量均计入本地汇总，但不代表交易所活动资格。",
            "",
        ]
    )
    return "\n".join(lines)


def write_maker_soak_report(
    payload: Mapping[str, Any],
    *,
    output_dir: Path = Path("artifacts/reports"),
    generated_at: datetime | None = None,
) -> Path:
    generated = generated_at or datetime.now(UTC)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"weex-demo-maker-soak-{generated.strftime('%Y%m%dT%H%M%SZ')}.md"
    path.write_text(build_maker_soak_report(payload, generated_at=generated), encoding="utf-8")
    return path

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def build_maker_run_report(
    payload: Mapping[str, Any],
    *,
    baseline_seconds: float | None = None,
    generated_at: datetime | None = None,
) -> str:
    """Build a credential-free technical audit report from one Maker batch result."""
    generated = (generated_at or datetime.now(UTC)).astimezone(UTC)
    plan = _mapping(payload.get("plan"))
    policy = _mapping(payload.get("policy"))
    legs = [dict(row) for row in payload.get("legs", []) if isinstance(row, Mapping)]
    target = _decimal(plan.get("target_quote") or plan.get("target_quote_volume"))
    volume = _decimal(payload.get("total_quote_volume"))
    elapsed = _float(payload.get("elapsed_seconds"))
    fills_required = _int(plan.get("fills"))
    final_position_raw = payload.get("final_position")
    active_orders_raw = payload.get("active_order_count")
    final_position = _float(final_position_raw)
    active_orders = _int(active_orders_raw)
    status = _text(payload.get("status"))
    reason = _text(payload.get("reason"))
    submissions = sum(_int(leg.get("submissions")) for leg in legs)
    policy_cancels = sum(_int(leg.get("cancels")) for leg in legs)
    venue_cancels = sum(_int(leg.get("venue_cancels")) for leg in legs)
    preflight_skips = sum(_int(leg.get("preflight_skips")) for leg in legs)
    observation_errors = sum(_int(leg.get("observation_errors")) for leg in legs)
    cancel_verification_attempts = sum(_int(leg.get("cancel_verification_attempts")) for leg in legs)
    cancel_verification_errors = sum(_int(leg.get("cancel_verification_errors")) for leg in legs)
    rejections = sum(_int(leg.get("post_only_rejections")) for leg in legs)
    maker_fills = sum(_int(leg.get("fill_count")) for leg in legs)
    baseline_delta = None if baseline_seconds is None else baseline_seconds - elapsed
    baseline_percent = (
        None
        if baseline_seconds is None or baseline_seconds <= 0
        else (baseline_seconds - elapsed) / baseline_seconds * 100
    )

    lines = [
        "# WEEX Demo 纯 Maker 成交量运行报告",
        "",
        (
            f"> 生成时间：{generated.strftime('%Y-%m-%d %H:%M:%S UTC')}。"
            "报告仅包含汇总指标，不包含凭据、账户号或完整订单流水。"
        ),
        "",
        "## 技术结论",
        "",
        _summary(status, reason, volume, target, elapsed, maker_fills, fills_required, rejections),
        "",
        "## 核心结果与基线",
        "",
        "本表用于判断本轮是否完成目标，以及速度变化来自成交效率还是减少了无效提交。",
        "",
        "| 指标 | 本轮 | 基线 | 变化 |",
        "|---|---:|---:|---:|",
        (
            f"| 总耗时 | {elapsed:.3f} s | {_seconds(baseline_seconds)} | "
            f"{_delta(baseline_delta, baseline_percent, comparable=status == 'completed')} |"
        ),
        f"| 成交量 | {_number(volume)} SUSDT | 10,068.2368 SUSDT | {_number(volume - Decimal('10068.2368'))} SUSDT |",
        f"| 提交次数 | {submissions} | 39 | {submissions - 39:+d} |",
        f"| 客户端撤单请求 | {policy_cancels} | 15 | {policy_cancels - 15:+d} |",
        f"| 交易所终态撤单 | {venue_cancels} | 14 | {venue_cancels - 14:+d} |",
        f"| 本地预检跳过 | {preflight_skips} | 未记录 | 不适用 |",
        f"| 状态查询瞬时错误 | {observation_errors} | 未记录 | 不适用 |",
        f"| 撤单终态确认查询 | {cancel_verification_attempts} | 未记录 | 不适用 |",
        f"| 撤单终态确认错误 | {cancel_verification_errors} | 未记录 | 不适用 |",
        f"| Post-Only 拒绝 | {rejections} | 14（历史重分类） | {rejections - 14:+d} |",
        f"| Maker 成交事件 | {maker_fills} | 13 | {maker_fills - 13:+d} |",
        "",
        "## 逐腿执行日志",
        "",
        "每腿是一笔开仓或平仓目标；成交量按该腿实际累计成交报价金额计算。",
        "",
        (
            "| 腿 | 动作 | 状态 | 耗时 ms | 提交 | 预检跳过 | 客户端撤单 | 交易所撤单 | "
            "撤单确认 | 确认错误 | Maker 成交事件 | 成交量 SUSDT | 终仓位 | 结果原因 |"
        ),
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for leg in legs:
        lines.append(
            "| {sequence} | {action} | {status} | {elapsed_ms} | {submissions} | {preflight_skips} | "
            "{cancels} | {venue_cancels} | {cancel_verification_attempts} | {cancel_verification_errors} | "
            "{fill_count} | {quote_volume} | {final_position} | {reason} |".format(
                sequence=_int(leg.get("sequence")),
                action=_escape(leg.get("action")),
                status=_escape(leg.get("status")),
                elapsed_ms=_int(leg.get("elapsed_ms")),
                submissions=_int(leg.get("submissions")),
                preflight_skips=_int(leg.get("preflight_skips")),
                cancels=_int(leg.get("cancels")),
                venue_cancels=_int(leg.get("venue_cancels")),
                cancel_verification_attempts=_int(leg.get("cancel_verification_attempts")),
                cancel_verification_errors=_int(leg.get("cancel_verification_errors")),
                fill_count=_int(leg.get("fill_count")),
                quote_volume=_number(_decimal(leg.get("quote_volume"))),
                final_position=_number(_decimal(leg.get("final_position"))),
                reason=_escape(leg.get("reason")),
            )
        )

    cancel_reasons = Counter()
    for leg in legs:
        for event in leg.get("events", []):
            if isinstance(event, Mapping) and event.get("event") in {"cancel", "post_only_rejection"}:
                cancel_reasons[_text(event.get("reason"))] += 1
    lines.extend(
        [
            "",
            "## 范围、指标与执行方法",
            "",
            (
                f"- 范围：WEEX Demo，{_escape(plan.get('symbol'))}，目标 {_number(target)} SUSDT，"
                f"{fills_required} 条开/平腿。"
            ),
            "- 纯 Maker：所有提交必须为 `POST_ONLY`，任何吃单成交或 Post-Only 拒绝都会令批次失败。",
            "- 成交量：开仓和平仓的实际成交报价金额均计入本地汇总；不代表交易所活动或等级规则一定认可。",
            "- 仓位约束：仅允许单一 LONG 仓位，每轮从空仓开始并以空仓、零活动订单结束。",
            (
                "- 节流：WEEX Demo 的订单提交之间至少间隔 10.1 秒；等待完成后重新读取盘口，"
                f"再计算挂单价格。本轮被动保护距离为 {_int(policy.get('passive_guard_ticks'))}–"
                f"{_int(policy.get('max_passive_guard_ticks') or policy.get('passive_guard_ticks'))} ticks，"
                f"接近截止时间时最低收窄到 {_int(policy.get('urgent_guard_ticks'))} ticks。"
            ),
            "- 提交预检：价格完成交易所精度规范化后再读取一次盘口；若会吃单，则不调用下单 API。",
            "- 订单恢复：网络结果不确定时只按客户端订单 ID 查询，不自动重提。",
            "",
            "## 错误、限制与鲁棒性检查",
            "",
            _anomaly_summary(status, reason, rejections, venue_cancels, cancel_reasons),
            "",
            "| 检查项 | 结果 |",
            "|---|---|",
            f"| 批次状态为 completed | {_pass(status == 'completed')} |",
            f"| 成交量达到目标 | {_pass(volume >= target)} |",
            f"| 完成 {fills_required} 条腿 | {_pass(len(legs) == fills_required)} |",
            f"| 全部成交均为 Maker | {_pass(bool(payload.get('maker_only')))} |",
            f"| Post-Only 拒绝为 0 | {_pass(rejections == 0)} |",
            f"| 最终仓位为 0 | {_pass(final_position_raw is not None and final_position == 0)} |",
            f"| 最终活动订单为 0 | {_pass(active_orders_raw is not None and active_orders == 0)} |",
            "",
            (
                "历史接口对 Demo 只提供订单级累计值，不能重建撮合队列位置；"
                "因此报告能审计提交、终态与成交量，不能证明交易所活动资格。"
            ),
            "",
            "## 建议的下一步",
            "",
            _next_step(status, submissions, rejections, policy_cancels, final_position, active_orders),
            "",
            "## 待确认问题",
            "",
            (
                "- WEEX 是否将 Demo 的 `CANCELED / COULD_NOT_FILL` 统一视为 Post-Only 拒绝，"
                "目前只能根据 4–8 ms 终态时间和零成交量推断。"
            ),
            "- 若要继续比较策略，需要保持相同目标、腿数、仓位上限和 120 秒单腿超时，避免把参数变化误当成速度提升。",
            "",
        ]
    )
    return "\n".join(lines)


def write_maker_run_report(
    payload: Mapping[str, Any],
    *,
    baseline_seconds: float | None = None,
    output_dir: Path = Path("artifacts/reports"),
    generated_at: datetime | None = None,
) -> Path:
    generated = generated_at or datetime.now(UTC)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"weex-demo-maker-{generated.strftime('%Y%m%dT%H%M%SZ')}.md"
    path.write_text(
        build_maker_run_report(payload, baseline_seconds=baseline_seconds, generated_at=generated),
        encoding="utf-8",
    )
    return path


def build_maker_soak_report(payload: Mapping[str, Any], *, generated_at: datetime | None = None) -> str:
    generated = (generated_at or datetime.now(UTC)).astimezone(UTC)
    rounds = [dict(row) for row in payload.get("rounds", []) if isinstance(row, Mapping)]
    requested = _int(payload.get("rounds_requested"))
    completed = _int(payload.get("rounds_completed"))
    lines = [
        "# WEEX Demo 纯 Maker 连续轮次报告",
        "",
        f"> 生成时间：{generated.strftime('%Y-%m-%d %H:%M:%S UTC')}。报告不包含账户、凭据或订单 ID。",
        "",
        "## 技术结论",
        "",
        (
            f"连续测试状态为 **{_escape(payload.get('status'))}**（`{_escape(payload.get('reason'))}`），"
            f"完成 {completed}/{requested} 轮，总成交量 {_escape(payload.get('total_quote_volume'))} SUSDT，"
            f"总耗时 {_float(payload.get('elapsed_seconds')):.3f} 秒。"
        ),
        "",
        "## 逐轮结果",
        "",
        (
            "| 轮次 | 状态 | 原因 | 成交量 SUSDT | 耗时 s | 提交 | 预检跳过 | "
            "查询错误 | 撤单确认 | 确认错误 | Post-Only 拒绝 | 最终仓位 | 活动订单 |"
        ),
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rounds:
        lines.append(
            (
                "| {round} | {status} | {reason} | {volume} | {elapsed:.3f} | {submissions} | {preflight} | "
                "{observations} | {cancel_verifications} | {cancel_errors} | {rejections} | "
                "{position} | {orders} |"
            ).format(
                round=_int(row.get("round")),
                status=_escape(row.get("status")),
                reason=_escape(row.get("reason")),
                volume=_escape(row.get("total_quote_volume")),
                elapsed=_float(row.get("elapsed_seconds")),
                submissions=_int(row.get("submission_count")),
                preflight=_int(row.get("preflight_skip_count")),
                observations=_int(row.get("observation_error_count")),
                cancel_verifications=_int(row.get("cancel_verification_attempt_count")),
                cancel_errors=_int(row.get("cancel_verification_error_count")),
                rejections=_int(row.get("post_only_rejection_count")),
                position=_escape(row.get("final_position")),
                orders=_escape(row.get("active_order_count")),
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
                    round=_int(row.get("round")),
                    leg=_int(leg.get("sequence")),
                    action=_escape(leg.get("action")),
                    status=_escape(leg.get("status")),
                    elapsed=_int(leg.get("elapsed_ms")),
                    submissions=_int(leg.get("submissions")),
                    preflight=_int(leg.get("preflight_skips")),
                    cancels=_int(leg.get("cancels")) + _int(leg.get("venue_cancels")),
                    cancel_verifications=_int(leg.get("cancel_verification_attempts")),
                    cancel_errors=_int(leg.get("cancel_verification_errors")),
                    fills=_int(leg.get("fill_count")),
                    volume=_number(_decimal(leg.get("quote_volume"))),
                    position=_number(_decimal(leg.get("final_position"))),
                )
            )
    all_flat = bool(rounds) and all(_float(row.get("final_position")) == 0 for row in rounds)
    no_orders = bool(rounds) and all(_int(row.get("active_order_count")) == 0 for row in rounds)
    lines.extend(
        [
            "",
            "## 验收检查",
            "",
            "| 检查项 | 结果 |",
            "|---|---|",
            f"| 完成全部 {requested} 轮 | {_pass(completed == requested)} |",
            f"| 每轮最终空仓 | {_pass(all_flat)} |",
            f"| 每轮最终无活动订单 | {_pass(no_orders)} |",
            f"| Post-Only 拒绝为 0 | {_pass(_int(payload.get('total_post_only_rejections')) == 0)} |",
            "",
            "连续测试在任一轮失败后立即停止，不会自动清仓后继续下一轮。开仓和平仓成交量均计入本地汇总，"
            "但不代表交易所活动资格。",
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


def _summary(
    status: str,
    reason: str,
    volume: Decimal,
    target: Decimal,
    elapsed: float,
    fills: int,
    fills_required: int,
    rejections: int,
) -> str:
    return (
        f"本轮状态为 **{_escape(status)}**（`{_escape(reason)}`），在 {elapsed:.3f} 秒内完成 "
        f"{_number(volume)} / {_number(target)} SUSDT，记录 {fills} / {fills_required} 个最低要求 Maker 成交事件，"
        f"Post-Only 拒绝 {rejections} 次。"
    )


def _anomaly_summary(
    status: str,
    reason: str,
    rejections: int,
    venue_cancels: int,
    cancel_reasons: Counter[str],
) -> str:
    reasons = ", ".join(f"`{_escape(key)}` × {value}" for key, value in sorted(cancel_reasons.items()))
    if status == "completed" and rejections == 0 and venue_cancels == 0:
        return "未发现交易所侧撤单、Post-Only 拒绝或批次级错误。策略主动撤单原因：" + (reasons or "无。")
    return (
        f"发现需要关注的终态：批次 `{_escape(status)}` / `{_escape(reason)}`，交易所撤单 {venue_cancels} 次，"
        f"Post-Only 拒绝 {rejections} 次。已记录原因：{reasons or '无可用原因。'}"
    )


def _next_step(
    status: str,
    submissions: int,
    rejections: int,
    policy_cancels: int,
    final_position: float,
    active_orders: int,
) -> str:
    if final_position != 0:
        return (
            f"当前仍有 {final_position:g} BTC Demo LONG、活动订单 {active_orders} 个。"
            "先用独立精确口令执行纯 Maker flatten；平仓完成前不启动新一轮成交量任务。"
        )
    if status != "completed":
        return "保持当前空仓空单，复核失败腿的盘口与终态原因；在原因不明确前不自动重跑。"
    if rejections > 0:
        return "保持 Post-Only 拒绝即停止的规则，继续减少提交前盘口读取与实际提交之间的延迟。"
    if submissions > 10 or policy_cancels > 0:
        return "下一轮优先减少策略主动撤单和额外提交；只有在相同约束下重复成功后，才把本轮速度视为可复现。"
    return "已经达到理论上的 10 次最少提交；下一轮只验证速度的可复现性，不再增加成交风险。"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except InvalidOperation:
        return Decimal("0")


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str:
    return "unknown" if value is None or value == "" else str(value)


def _escape(value: Any) -> str:
    return _text(value).replace("|", "\\|").replace("\n", " ")


def _number(value: Decimal) -> str:
    return f"{value:f}"


def _seconds(value: float | None) -> str:
    return "未提供" if value is None else f"{value:.3f} s"


def _delta(delta: float | None, percent: float | None, *, comparable: bool) -> str:
    if not comparable:
        return "不可比（本轮未完成）"
    if delta is None or percent is None:
        return "未比较"
    direction = "更快" if delta > 0 else "更慢"
    return f"{abs(delta):.3f} s {direction}（{abs(percent):.1f}%）"


def _pass(value: bool) -> str:
    return "通过" if value else "失败"

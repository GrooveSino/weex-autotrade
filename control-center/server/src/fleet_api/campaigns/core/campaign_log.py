from __future__ import annotations

import re
from collections.abc import Mapping

from weex_cli.execution_progress import describe_execution_event

from fleet_api.models import LogLevel

_SAFE_TEXT = re.compile(r"[^A-Za-z0-9._:/+\- ]+")


def campaign_event_log(event: Mapping[str, object]) -> tuple[LogLevel, str] | None:
    """Render a persisted Campaign event for the account's on-demand log stream.

    Campaign journal events deliberately contain only a small safe projection of
    execution state.  This formatter keeps that boundary: it never serializes an
    arbitrary event payload, order response, credential, or proxy value.
    """
    name = _text(event.get("name"), limit=96) or "event"
    fields = event.get("fields")
    values = fields if isinstance(fields, Mapping) else {}

    def value(key: str, *, fallback: str = "-") -> str:
        return _text(values.get(key), limit=80) or fallback

    def core(key: str, *, fallback: str = "-") -> str:
        return _text(event.get(key), limit=80) or fallback

    run = core("run")
    if name == "campaign_run_started":
        return LogLevel.INFO, f"实盘执行：运行 {run} 开始；剩余目标 {value('remaining_quote')} USDT"
    if name == "campaign_run_completed":
        return (
            LogLevel.SUCCESS,
            f"实盘执行：运行 {run} 已完成；本次 {value('child_quote')} USDT，累计 {value('total_quote')} USDT",
        )
    if name in {"phase_pacing_started", "phase_pacing_completed", "phase_pacing_cancelled"}:
        phase = "开仓" if core("phase") == "open" else "平仓"
        if name == "phase_pacing_started":
            return LogLevel.INFO, f"实盘执行：全局执行错峰；第 {value('round')} 轮 {phase} 等待槽位"
        if name == "phase_pacing_completed":
            return LogLevel.SUCCESS, f"实盘执行：全局执行错峰完成；第 {value('round')} 轮准备{phase}"
        return LogLevel.WARN, f"实盘执行：全局执行错峰已取消；{value('reason')}"
    if name in {"campaign_boundary_started", "campaign_boundary_completed"}:
        action = "正在核验账户边界" if name.endswith("started") else "账户边界核验完成"
        return (LogLevel.INFO if name.endswith("started") else LogLevel.SUCCESS, f"实盘执行：{action}")
    if name in {"campaign_child_planning_started", "campaign_child_planning_completed"}:
        action = "正在生成本轮 BTC/ETH 子计划" if name.endswith("started") else "本轮 BTC/ETH 子计划已生成"
        detail = value("remaining_quote") if name.endswith("started") else core("child_plan_id")
        return (LogLevel.INFO if name.endswith("started") else LogLevel.SUCCESS, f"实盘执行：{action}；{detail}")
    if name == "campaign_read_retry":
        return LogLevel.WARN, f"实盘执行：读取重试 {value('attempt')}；{value('operation')}，等待 {value('seconds')}s"
    if name == "campaign_finished":
        level = LogLevel.SUCCESS if core("status") == "completed" else LogLevel.WARN
        return level, f"实盘执行：Campaign {core('status')}；累计 {value('total_quote')} USDT；{value('reason')}"
    if name in {"preflight_started", "preflight_completed", "preflight_rejected", "preflight_retry"}:
        if name == "preflight_started":
            return LogLevel.INFO, "实盘执行：正在进行执行前检查（余额、持仓、挂单与 Beta）"
        if name == "preflight_completed":
            return LogLevel.SUCCESS, "实盘执行：执行前检查完成，账户边界已通过"
        if name == "preflight_rejected":
            return LogLevel.WARN, f"实盘执行：执行前检查未通过；{value('reason')}"
        return LogLevel.WARN, f"实盘执行：执行前检查读取重试 {value('attempt')}，等待 {value('seconds')}s"
    if name == "cycle_started":
        return LogLevel.INFO, f"实盘执行：第 {value('round')} 轮开始；计划总交易量 {value('desired_quote')} USDT"
    if name == "cycle_preparing":
        return LogLevel.INFO, f"实盘执行：第 {value('round')} 轮正在读取 BTC/ETH 盘口并计算数量"
    if name == "open_barrier_verified":
        return LogLevel.SUCCESS, f"实盘执行：BTC/ETH 本轮目标仓位已核验；第 {value('round')} 轮，开始持仓计时"
    if name == "open_barrier_not_ready":
        return LogLevel.WARN, f"实盘执行：BTC/ETH 本轮目标仓位未达成；第 {value('round')} 轮，不开始持仓计时"
    if name in {"hold_started", "hold_completed", "round_gap_started", "round_gap_completed"}:
        labels = {
            "hold_started": "BTC/ETH 双边开仓完成，进入持仓等待",
            "hold_completed": "双边持仓等待完成，准备平仓",
            "round_gap_started": "本轮完成，进入轮次间隔",
            "round_gap_completed": "轮次间隔完成，准备下一轮",
        }
        return LogLevel.INFO, f"实盘执行：{labels[name]}；第 {value('round')} 轮 / {value('seconds')}s"
    if name in {"pair_wait_started", "pair_wait_completed", "close_barrier_started"}:
        labels = {
            "pair_wait_started": "正在等待 BTC/ETH 两腿进入确定状态",
            "pair_wait_completed": "BTC/ETH 两腿状态已核验",
            "close_barrier_started": "开仓阶段结束，正在读取持仓并准备双腿平仓",
        }
        return (LogLevel.SUCCESS if name == "pair_wait_completed" else LogLevel.INFO, f"实盘执行：{labels[name]}")
    if name in {"accounting_waiting", "accounting_retry_wait", "accounting_wait_completed"}:
        if name == "accounting_waiting":
            return (
                LogLevel.INFO,
                "实盘执行：等待成交明细对账；"
                f"{value('symbol')} {value('action')}，第 {value('attempt')}/{value('max_attempts')} 次",
            )
        if name == "accounting_retry_wait":
            return LogLevel.WARN, f"实盘执行：成交明细尚未完整，{value('symbol')} 将在 {value('seconds')}s 后重查"
        return LogLevel.SUCCESS, f"实盘执行：成交明细对账完成；{value('symbol')} {core('status')}"
    if name in {"leg_completed", "leg_stopped", "leg_uncertain"}:
        if name == "leg_completed":
            return (
                LogLevel.SUCCESS,
                "实盘执行："
                f"{value('symbol')} {value('action')} 成交已核验；"
                f"{value('quote_volume')} USDT / {value('fill_count')} 笔",
            )
        state = "已安全停止" if name == "leg_stopped" else "结果待后台只读核验"
        return (
            LogLevel.ERROR if name == "leg_stopped" else LogLevel.WARN,
            f"实盘执行：{value('symbol')} {value('action')} {state}；{value('reason')}",
        )
    if name in {"cycle_completed", "cycle_stopped"}:
        level = LogLevel.SUCCESS if name == "cycle_completed" else LogLevel.WARN
        return (
            level,
            f"实盘执行：第 {value('round')} 轮 {core('status')}；"
            f"本轮 {value('quote_volume')} USDT，累计 {value('total_quote')} USDT",
        )
    if name in {"final_acceptance_started", "final_acceptance_completed"}:
        if name.endswith("started"):
            return LogLevel.INFO, f"实盘执行：正在最终验收；当前累计 {value('total_quote')} USDT"
        completed = value("completed") == "True"
        return (
            LogLevel.SUCCESS if completed else LogLevel.WARN,
            "实盘执行：最终验收"
            f"{'通过' if completed else '未通过'}；空仓={value('flat')}，"
            f"无挂单={value('no_orders')}，Maker={value('maker_only')}",
        )
    if name in {"workflow_finished", "campaign_uncertain"}:
        if name == "campaign_uncertain":
            return LogLevel.WARN, "实盘执行：工作线程结果待核验，正在后台只读恢复"
        level = LogLevel.SUCCESS if core("status") == "completed" else LogLevel.WARN
        return level, f"实盘执行：流程 {core('status')}；已核验 {value('executed_quote_volume')} USDT"
    if name == "campaign_recovering":
        reason = value("reason")
        message = "仓位数量格式异常，已进入恢复检查" if "typeerror" in reason else "执行阶段异常，已进入恢复检查"
        return LogLevel.WARN, f"实盘执行：{message}；错误编号 {value('error_id')}"
    presentation = describe_execution_event(event)
    if presentation is None:
        return None
    return LogLevel(presentation.level), f"实盘执行：{presentation.message}"


def _text(raw: object, *, limit: int) -> str:
    if raw is None:
        return ""
    rendered = _SAFE_TEXT.sub("", str(raw)).strip()
    return rendered[:limit]

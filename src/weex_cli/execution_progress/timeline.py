"""Human-readable execution timeline messages."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import (
    _UNCERTAIN_REASON_ZH,
    TimelinePresentation,
    action_label,
    condition_presentation,
    event_name,
    event_value,
    status_label,
)


def describe_execution_event(event: Mapping[str, Any]) -> TimelinePresentation | None:
    name = event_name(event)
    value = lambda key, default="-": event_value(event, key, default)  # noqa: E731
    run = value("run")
    round_number = value("round")
    symbol = value("symbol", "")
    action = action_label(value("action", ""))

    if name == "campaign_run_started":
        return TimelinePresentation("info", f"Campaign 运行 {run} 开始", f"剩余 {value('remaining_quote')} USDT")
    if name == "campaign_run_completed":
        return TimelinePresentation(
            "success",
            f"Campaign 运行 {run} 已保存检查点",
            f"本次 {value('child_quote')} USDT / 累计 {value('total_quote')} USDT",
        )
    if name == "phase_pacing_completed":
        return TimelinePresentation(
            "success",
            "全局执行错峰完成",
            f"第 {round_number} 轮 / {action_label(value('phase'))}",
        )
    if name == "phase_pacing_cancelled":
        return TimelinePresentation("warn", "全局执行错峰已取消", str(value("reason")))
    if name == "campaign_boundary_completed":
        return TimelinePresentation(
            "success", "账户边界读取完成", "启动前" if value("phase", "") == "initial" else "周期检查点"
        )
    if name == "campaign_child_planning_completed":
        return TimelinePresentation("success", "BTC/ETH 子计划生成完成", f"运行 {run} / {value('child_plan_id')}")
    if name == "campaign_read_retry":
        return TimelinePresentation(
            "warn",
            "Campaign 只读检查失败，等待重试",
            f"第 {value('attempt')}/{value('max_attempts')} 次 / {value('seconds')}s",
        )
    if name == "campaign_finished":
        level = "success" if value("status", "") == "completed" else "warn"
        return TimelinePresentation(
            level,
            f"Campaign {status_label(value('status'))}",
            f"累计 {value('total_quote')} USDT / {value('reason')}",
        )
    if name == "preflight_completed":
        return TimelinePresentation("success", "执行前检查完成", "账户已就绪并确认空仓")
    if name == "preflight_rejected":
        return TimelinePresentation("error", "执行前检查未通过", str(value("reason")))
    if name == "preflight_retry":
        return TimelinePresentation(
            "warn",
            "执行前检查读取失败，等待重试",
            f"第 {value('attempt')} 次 / {value('seconds')}s / {value('error', value('reason'))}",
        )
    if name == "cycle_started":
        return TimelinePresentation(
            "info",
            f"第 {round_number} 轮开始",
            f"本轮完整交易量 {value('planned_turnover_quote', value('desired_quote'))} USDT / "
            f"开仓名义 {value('opening_notional_quote')} USDT / {value('leverage')}x",
        )
    if name == "cycle_plan_created":
        return TimelinePresentation(
            "info",
            f"第 {round_number} 轮执行快照已冻结",
            f"第 {value('attempt')} 次尝试 / Beta {value('beta_version')} / "
            f"完整交易量 {value('planned_turnover_quote', value('desired_quote'))} USDT",
        )
    if name == "condition_waiting":
        title, action = condition_presentation(value("condition"))
        return TimelinePresentation("warn", title, action)
    if name == "condition_wait_resumed":
        return TimelinePresentation("success", "执行条件已恢复", "系统正在按最新 Beta 与行情生成下一轮。")
    if name == "cycle_leverage_ready":
        return TimelinePresentation("success", "BTC/ETH 杠杆准备完成", f"{value('leverage')}x")
    if name in {"cycle_sizing_retry", "cycle_read_retry"}:
        label = "盘口读取失败，等待重新计算数量" if name == "cycle_sizing_retry" else "账户参数读取失败，等待重查"
        return TimelinePresentation(
            "warn", label, f"{symbol} / 第 {value('attempt')}/{value('max_attempts')} 次 / {value('seconds')}s"
        )
    if name == "leg_started":
        return TimelinePresentation(
            "info", f"{symbol} {action}准备完成", f"{action_label(value('side'))} {value('quantity')} / POST_ONLY"
        )
    if name == "leg_completed":
        return TimelinePresentation(
            "success", f"{symbol} {action}成交已核验", f"{value('quote_volume')} USDT / {value('fill_count')} 笔"
        )
    if name == "dust_close_detected":
        reasons = {
            "below_minimum": "低于交易所最小下单量",
            "minimum_rejected": "交易所明确拒绝最小量或精度",
            "quote_threshold": "当前任务剩余仓位不超过尾仓阈值",
        }
        return TimelinePresentation(
            "warn",
            f"{symbol} 检测到当前任务小额尾仓",
            f"{value('quantity')} / 约 {value('quote')} USDT / {reasons.get(str(value('reason')), value('reason'))}",
        )
    if name == "market_close_intent_persisted":
        return TimelinePresentation("warn", f"{symbol} 市价收尾意图已持久化", "同一任务交易腿最多提交一次")
    if name == "market_close_accepted":
        return TimelinePresentation("info", f"{symbol} 小额尾仓市价平仓已受理", "正在只读核验仓位与成交")
    if name == "market_close_verified":
        verified = bool(value("verified", False))
        return TimelinePresentation(
            "success" if verified else "warn",
            f"{symbol} 小额尾仓已清零",
            (
                f"成交已核验 / {value('quote_volume')} USDT / {value('fill_count')} 笔"
                if verified
                else "仓位已清零，成交账本稍后继续只读核验"
            ),
        )
    if name == "market_close_uncertain":
        return TimelinePresentation("error", f"{symbol} 小额尾仓市价平仓结果待核验", str(value("reason")))
    if name in {"leg_stopped", "leg_uncertain"}:
        title = f"{symbol} {action}{'已安全停止' if name == 'leg_stopped' else '状态不确定'}"
        return TimelinePresentation("error" if name == "leg_stopped" else "warn", title, str(value("reason")))
    if name == "position_observation_unavailable":
        return TimelinePresentation("error", f"{symbol} {action}仓位读取失败", "已停止该通道继续下单")
    if name == "pair_wait_completed":
        return TimelinePresentation("success", "BTC/ETH 双腿屏障已通过", f"第 {round_number} 轮 / {action}")
    if name == "open_barrier_verified":
        return TimelinePresentation("success", "BTC/ETH 目标仓位已核验", f"第 {round_number} 轮 / 开始持仓计时")
    if name == "open_barrier_not_ready":
        return TimelinePresentation("warn", "BTC/ETH 目标仓位未达成", f"第 {round_number} 轮 / 不开始持仓计时")
    if name == "close_barrier_started":
        return TimelinePresentation("info", "开仓阶段结束", "正在读取实际持仓并准备并发平仓")
    if name == "accounting_wait_completed":
        return TimelinePresentation("success", f"{symbol} 成交明细对账完成", str(value("status")))
    if name == "accounting_retry_wait":
        return TimelinePresentation(
            "warn",
            f"{symbol} 成交明细尚未完整",
            f"{value('seconds')}s 后第 {value('attempt')}/{value('max_attempts')} 次重查",
        )
    if name == "hold_completed":
        return TimelinePresentation("success", "双边持仓等待完成", f"第 {round_number} 轮 / {value('seconds')}s")
    if name == "round_gap_completed":
        return TimelinePresentation("success", "轮次间隔完成", f"第 {round_number} 轮后 / {value('seconds')}s")
    if name == "final_acceptance_completed":
        passed = bool(value("completed", False))
        return TimelinePresentation(
            "success" if passed else "warn",
            "最终验收通过" if passed else "最终验收未通过",
            f"空仓={value('flat')} / 无挂单={value('no_orders')} / 流动性策略={value('liquidity_policy_satisfied')}",
        )
    if name in {"cycle_completed", "cycle_stopped"}:
        return TimelinePresentation(
            "success" if name == "cycle_completed" else "warn",
            f"第 {round_number} 轮 {value('status')}",
            f"本轮 {value('quote_volume')} USDT / 累计 {value('total_quote')} USDT",
        )
    if name == "workflow_finished":
        level = "success" if value("status", "") == "completed" else "warn"
        return TimelinePresentation(
            level,
            f"执行流程 {status_label(value('status'))}",
            f"已核验 {value('executed_quote_volume')} USDT / {value('reason')}",
        )
    if name == "campaign_uncertain":
        reason = str(value("reason") or "")
        detail = _UNCERTAIN_REASON_ZH.get(reason, "需要人工核对仓位、挂单和成交明细")
        return TimelinePresentation("warn", "执行结果不确定", detail)
    if name == "safe_stop_started":
        return TimelinePresentation("warn", "安全停止已接管", "正在撤销 BTC/ETH 常规单与条件单")
    if name == "safe_stop_cancel_verified":
        return TimelinePresentation("success", f"{symbol} 撤单已核验", "优先进入 Maker 平仓")
    if name == "safe_stop_cancel_unverified":
        return TimelinePresentation("error", f"{symbol} 撤单未能核验", "停止自动平仓，需人工核对挂单和仓位")
    if name == "safe_stop_flattening":
        return TimelinePresentation("warn", f"{symbol} 正在优先使用 Maker 平仓", f"当前任务仓位 {value('quantity')}")
    if name == "safe_stop_leg_completed":
        return TimelinePresentation("success", f"{symbol} Maker-only 平仓已完成")
    if name == "safe_stop_verified":
        return TimelinePresentation("success", "安全停止已核验", "BTC/ETH 已空仓且无活动委托")
    if name == "safe_stop_uncertain":
        return TimelinePresentation("error", "安全停止结果待核验", str(value("reason")))
    if name == "campaign_reconciliation_acknowledged":
        return TimelinePresentation("success", "人工对账已记录")
    if name != "leg_progress":
        return None

    progress = str(value("progress_event", ""))
    prefix = f"{symbol} {action}".strip()
    if progress == "market_data_source":
        source = value("source", "rest")
        return TimelinePresentation(
            "success" if source == "websocket" else "warn",
            f"{prefix}盘口来源",
            "WebSocket 实时深度" if source == "websocket" else "REST 安全回退",
        )
    if progress == "submit":
        return TimelinePresentation(
            "info", f"{prefix} Maker 挂单已提交", f"价格 {value('price')} / 数量 {value('quantity')}"
        )
    if progress == "fill":
        return TimelinePresentation(
            "success", f"{prefix}观察到 Maker 成交", f"数量 {value('quantity')} / {value('quote')} USDT"
        )
    if progress == "cancel_started":
        return TimelinePresentation("warn", f"{prefix}报价需要更新", "正在撤单并确认结果")
    if progress == "cancel":
        return TimelinePresentation("success", f"{prefix}撤单已确认", f"准备重新报价 / {value('reason')}")
    if progress == "preflight_skip":
        return TimelinePresentation("warn", f"{prefix}本地报价检查未通过", str(value("reason")))
    if progress == "order_terminal":
        return TimelinePresentation("info", f"{prefix}挂单已进入终态", str(value("status")))
    if progress == "timeout_cleanup_started":
        return TimelinePresentation("warn", f"{prefix}达到腿超时", "正在取消普通单和条件单")
    if progress == "timeout_cleanup_confirmed":
        return TimelinePresentation("success", f"{prefix}超时清理已确认", "允许读取残仓并进行 Maker 平仓")
    if progress in {"timeout_cleanup_not_confirmed", "timeout_cleanup_error", "timeout_order_not_confirmed"}:
        return TimelinePresentation("error", f"{prefix}超时状态未能确认", "禁止继续下单并进入人工核验")
    return None

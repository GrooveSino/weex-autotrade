"""Execution-progress state contracts and shared presentation helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

WAITING_LABELS_ZH = {
    "maker_fill": "等待 Maker 挂单成交",
    "cancel_confirmation": "等待撤单最终状态",
    "order_observation_retry": "等待重新读取订单状态",
    "position_observation_retry": "等待重新读取仓位",
    "market_observation_retry": "等待重新读取盘口",
    "submission_slot": "等待下单限频窗口",
    "submission_preflight_retry": "等待重新计算 Maker 报价",
    "submission_recovery": "按客户订单号确认下单结果",
    "submission_verification": "重新验证下单状态",
    "submission_post_only_verification": "确认订单保持 POST_ONLY",
    "submission_book_check": "重新读取下单前盘口",
    "amount_precision": "重新读取数量精度",
    "price_precision": "重新读取价格精度",
    "cleanup_order_observation": "重新读取清理后的委托",
    "cleanup_order_clearance": "等待清理后的委托消失",
    "precheck_positions": "重新读取下单前仓位",
    "precheck_open_orders": "重新读取下单前委托",
    "order_identity": "确认成交订单身份",
    "fill_reconciliation": "等待 WEEX 成交明细对账",
    "open_order_clearance": "确认无残留挂单",
}

EXECUTION_PROGRESS_PROJECTION_VERSION = 7

_ACTION_ZH = {"open": "开仓", "close": "平仓", "buy": "买入", "sell": "卖出"}
_STATUS_ZH = {
    "completed": "已完成",
    "stopped": "已停止",
    "uncertain": "结果不确定",
    "failed": "失败",
    "executing": "执行中",
}
_UNCERTAIN_REASON_ZH = {
    "worker_safety:available_balance_insufficient": "可用余额不足以覆盖本轮保证金",
    "worker_safety:account_boundary_not_flat": "发现持仓或挂单，账户边界不满足启动条件",
    "worker_safety:timing_policy_unavailable": "策略时间参数不可用",
    "worker_safety:beta_source_unavailable": "Final Beta 数据源不可用",
    "worker_safety:authorization_expired": "启动确认已过期，需要重新预览",
    "worker_safety:leverage_verification_failed": "杠杆设置或核验未通过",
    "worker_safety:post_only_verification_failed": "POST_ONLY 状态核验未通过",
    "worker_safety:preflight_rejected": "执行前安全核验未通过",
}
_CONDITION_ZH = {
    "account_read_retry": ("账户条件暂时无法读取", "系统会自动重新核验账户；无需重新启动。"),
    "beta_unavailable": ("最新 Beta 暂不可用", "系统会自动读取最新 Beta 后继续。"),
    "empty_cycle": ("本轮未形成可核验成交", "系统会使用新的行情快照自动继续尝试。"),
    "external_account_boundary": ("账户存在来源不明的仓位或挂单", "请清理这些仓位或挂单；系统会自动复查并继续。"),
    "insufficient_available_margin": ("可用保证金暂不足", "补足可用保证金后，系统会自动继续。"),
    "maker_attempt_unavailable": ("本轮 Maker 条件暂不可用", "系统会按最新行情重新计算后继续。"),
    "owned_close_maker_retry": (
        "本任务仓位的 Maker 平仓暂不可成交",
        "系统会重新读取盘口并继续被动平仓；不会追价或改用普通市价单。",
    ),
    "minimum_order_infeasible": ("当前最小下单量条件暂不满足", "系统会等待价格或目标条件变化后重新计算。"),
    "persistence_unavailable": ("本地执行记录暂不可写入", "系统会等待本地记录恢复；在此之前不会下单。"),
    "shared_market_unavailable": ("共享 BTC/ETH 行情暂不可用", "系统会等待共享行情恢复后继续。"),
}


@dataclass(frozen=True)
class ActiveWait:
    key: str
    label: str
    updated_at_ms: int
    elapsed_ms: int = 0
    remaining_ms: int | None = None
    detail: str = ""
    symbol: str | None = None
    action: str | None = None
    started_at_ms: int | None = None
    deadline_at_ms: int | None = None


@dataclass(frozen=True)
class TimelinePresentation:
    level: str
    title: str
    detail: str = ""

    @property
    def message(self) -> str:
        return f"{self.title}；{self.detail}" if self.detail else self.title


def event_name(event: Mapping[str, Any]) -> str:
    return str(event.get("event") or event.get("name") or "")


def event_value(event: Mapping[str, Any], key: str, default: Any = None) -> Any:
    if key in event:
        return event[key]
    fields = event.get("fields")
    if isinstance(fields, Mapping) and key in fields:
        return fields[key]
    return default


def action_label(value: Any) -> str:
    text = str(value or "")
    return _ACTION_ZH.get(text, text.replace("_", " "))


def status_label(value: Any) -> str:
    rendered = str(value or "")
    return _STATUS_ZH.get(rendered, rendered.replace("_", " "))


def condition_presentation(value: Any) -> tuple[str, str]:
    return _CONDITION_ZH.get(
        str(value or ""),
        ("执行条件暂不可用", "系统会自动重新检查；也可以随时安全停止。"),
    )


def execution_phase(event: Mapping[str, Any]) -> str | None:
    name = event_name(event)
    if name == "actor_lifecycle":
        return None
    if name == "condition_waiting":
        return "条件等待"
    if name == "condition_wait_resumed":
        return "重新生成下一轮"
    if name.startswith("dust_close") or name.startswith("market_close"):
        return "小额尾仓收敛"
    if name.startswith("safe_stop"):
        return "安全停止"
    if name.startswith("campaign_boundary"):
        return "账户边界核验"
    if name.startswith("final_acceptance"):
        return "最终验收"
    if name == "campaign_run_started":
        return "任务启动"
    if name == "campaign_run_completed":
        return "运行收尾"
    if name.startswith("campaign_child_planning") or name in {"cycle_preparing", "cycle_sizing_retry"}:
        return "轮次规划"
    if name.startswith("preflight"):
        return "执行前检查"
    if name.startswith("leverage") or name == "cycle_leverage_ready":
        return "杠杆准备"
    if name == "cycle_started":
        return "开仓执行"
    if name in {"cycle_completed", "cycle_stopped"}:
        return "轮次收尾"
    if name.startswith("close_barrier"):
        return "平仓执行"
    if name.startswith("leg"):
        symbol = event_value(event, "symbol", "")
        return f"{symbol} {action_label(event_value(event, 'action'))}".strip()
    if name.startswith("pair_wait") or name.startswith("open_barrier"):
        action = str(event_value(event, "action", ""))
        return "双腿平仓核验" if action == "close" else "双腿开仓核验"
    if name.startswith("accounting"):
        return "成交明细对账"
    if name == "hold_started":
        return "持仓等待"
    if name == "hold_completed":
        return "准备平仓"
    if name == "round_gap_started":
        return "轮次间隔"
    if name == "round_gap_completed":
        return "准备下一轮"
    if name.startswith("phase_pacing"):
        return "全局执行错峰"
    if name in {"campaign_finished", "workflow_finished"}:
        return "任务完成"
    return None

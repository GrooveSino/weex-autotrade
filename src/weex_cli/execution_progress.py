from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
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

EXECUTION_PROGRESS_PROJECTION_VERSION = 3

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
    "worker_safety:beta_changed_since_preview": "Final Beta 已变化，需要重新预览",
    "worker_safety:authorization_expired": "启动确认已过期，需要重新预览",
    "worker_safety:leverage_verification_failed": "杠杆设置或核验未通过",
    "worker_safety:post_only_verification_failed": "POST_ONLY 状态核验未通过",
    "worker_safety:preflight_rejected": "执行前安全核验未通过",
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


def execution_phase(event: Mapping[str, Any]) -> str:
    name = event_name(event)
    if name.startswith("safe_stop"):
        return "安全停止"
    if name.startswith("campaign_boundary") or name.startswith("final_acceptance"):
        return "账户边界核验"
    if name.startswith("campaign_child_planning") or name in {"cycle_preparing", "cycle_sizing_retry"}:
        return "轮次规划"
    if name.startswith("preflight"):
        return "执行前检查"
    if name.startswith("leverage") or name == "cycle_leverage_ready":
        return "杠杆准备"
    if name.startswith("leg"):
        symbol = event_value(event, "symbol", "")
        return f"{symbol} {action_label(event_value(event, 'action'))}".strip()
    if name.startswith("pair_wait"):
        return "双腿状态核验"
    if name.startswith("accounting"):
        return "成交明细对账"
    if name.startswith("hold"):
        return "持仓等待"
    if name.startswith("round_gap"):
        return "轮次间隔"
    if name in {"campaign_finished", "workflow_finished"}:
        return "任务完成"
    return name.replace("_", " ")


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
            f"目标 {value('desired_quote')} USDT / "
            f"BTC {value('btc_quantity')} + ETH {value('eth_quantity')} / {value('leverage')}x",
        )
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
    if name in {"leg_stopped", "leg_uncertain"}:
        title = f"{symbol} {action}{'已安全停止' if name == 'leg_stopped' else '状态不确定'}"
        return TimelinePresentation("error" if name == "leg_stopped" else "warn", title, str(value("reason")))
    if name == "position_observation_unavailable":
        return TimelinePresentation("error", f"{symbol} {action}仓位读取失败", "已停止该通道继续下单")
    if name == "pair_wait_completed":
        return TimelinePresentation("success", "BTC/ETH 双腿屏障已通过", f"第 {round_number} 轮 / {action}")
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
            f"空仓={value('flat')} / 无挂单={value('no_orders')} / Maker={value('maker_only')}",
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
        return TimelinePresentation("success", f"{symbol} 撤单已核验", "可以进入 Maker-only 平仓")
    if name == "safe_stop_cancel_unverified":
        return TimelinePresentation("error", f"{symbol} 撤单未能核验", "停止自动平仓，需人工核对挂单和仓位")
    if name == "safe_stop_flattening":
        return TimelinePresentation("warn", f"{symbol} 正在 Maker-only 平仓", f"残仓 {value('quantity')}" )
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


class ExecutionProgressProjector:
    def __init__(self) -> None:
        self.active_waits: dict[str, ActiveWait] = {}
        self.phase = "启动"
        self.current_run = 0
        self.current_round = 0
        self.submissions = 0
        self.cancels = 0
        self.requotes = 0
        self.execution_verified_quote_volume = Decimal(0)
        self.btc_quote_volume = Decimal(0)
        self.eth_quote_volume = Decimal(0)
        self._current_run_base_quote = Decimal(0)
        self._completed_leg_quotes: dict[str, Decimal] = {}

    def apply(self, event: Mapping[str, Any], *, at_ms: int) -> TimelinePresentation | None:
        self.phase = execution_phase(event)
        run = event_value(event, "run")
        round_number = event_value(event, "round")
        if run is not None:
            self.current_run = max(self.current_run, int(run))
        if round_number is not None:
            self.current_round = max(self.current_round, int(round_number))
        self._update_volume(event)
        consumed = self._update_waits(event, at_ms)
        self._update_counts(event)
        return None if consumed else describe_execution_event(event)

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": EXECUTION_PROGRESS_PROJECTION_VERSION,
            "phase": self.phase,
            "current_run": self.current_run,
            "current_round": self.current_round,
            "submissions": self.submissions,
            "cancels": self.cancels,
            "requotes": self.requotes,
            "execution_verified_quote_volume": format(self.execution_verified_quote_volume, "f"),
            "btc_quote_volume": format(self.btc_quote_volume, "f"),
            "eth_quote_volume": format(self.eth_quote_volume, "f"),
            "active_waits": [asdict(wait) for wait in self.active_waits.values()],
            "current_run_base_quote": format(self._current_run_base_quote, "f"),
            "completed_leg_quotes": {
                key: format(value, "f") for key, value in self._completed_leg_quotes.items()
            },
        }

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any] | None) -> ExecutionProgressProjector:
        projector = cls()
        if not isinstance(snapshot, Mapping):
            return projector
        projector.phase = str(snapshot.get("phase") or projector.phase)
        projector.current_run = _nonnegative_int(snapshot.get("current_run"))
        projector.current_round = _nonnegative_int(snapshot.get("current_round"))
        projector.submissions = _nonnegative_int(snapshot.get("submissions"))
        projector.cancels = _nonnegative_int(snapshot.get("cancels"))
        projector.requotes = _nonnegative_int(snapshot.get("requotes"))
        projector.execution_verified_quote_volume = _decimal_or_zero(
            snapshot.get("execution_verified_quote_volume")
        )
        projector.btc_quote_volume = _decimal_or_zero(snapshot.get("btc_quote_volume"))
        projector.eth_quote_volume = _decimal_or_zero(snapshot.get("eth_quote_volume"))
        projector._current_run_base_quote = _decimal_or_zero(snapshot.get("current_run_base_quote"))
        completed = snapshot.get("completed_leg_quotes")
        if isinstance(completed, Mapping):
            projector._completed_leg_quotes = {
                str(key): parsed
                for key, value in completed.items()
                if (parsed := _nonnegative_decimal(value)) is not None
            }
        waits = snapshot.get("active_waits")
        if isinstance(waits, list):
            for raw in waits:
                if not isinstance(raw, Mapping) or not raw.get("key"):
                    continue
                try:
                    wait = ActiveWait(
                        key=str(raw["key"]),
                        label=str(raw.get("label") or raw["key"]),
                        updated_at_ms=int(raw.get("updated_at_ms") or 0),
                        elapsed_ms=_nonnegative_int(raw.get("elapsed_ms")),
                        remaining_ms=(
                            None if raw.get("remaining_ms") is None else _nonnegative_int(raw.get("remaining_ms"))
                        ),
                        detail=str(raw.get("detail") or ""),
                        symbol=str(raw["symbol"]) if raw.get("symbol") else None,
                        action=str(raw["action"]) if raw.get("action") else None,
                        started_at_ms=(
                            None if raw.get("started_at_ms") is None else int(raw["started_at_ms"])
                        ),
                        deadline_at_ms=(
                            None if raw.get("deadline_at_ms") is None else int(raw["deadline_at_ms"])
                        ),
                    )
                except (TypeError, ValueError):
                    continue
                projector.active_waits[wait.key] = wait
        return projector

    def _set_wait(self, wait: ActiveWait) -> None:
        previous = self.active_waits.get(wait.key)
        if wait.started_at_ms is None:
            started_at_ms = (
                previous.started_at_ms
                if previous is not None and previous.started_at_ms is not None
                else max(0, wait.updated_at_ms - wait.elapsed_ms)
            )
        else:
            started_at_ms = wait.started_at_ms
        deadline_at_ms = wait.deadline_at_ms
        if deadline_at_ms is None and wait.remaining_ms is not None:
            deadline_at_ms = wait.updated_at_ms + wait.remaining_ms
        self.active_waits[wait.key] = ActiveWait(
            **{
                **asdict(wait),
                "started_at_ms": started_at_ms,
                "deadline_at_ms": deadline_at_ms,
            }
        )

    def _update_volume(self, event: Mapping[str, Any]) -> None:
        name = event_name(event)
        if name == "campaign_run_started":
            self._current_run_base_quote = self.execution_verified_quote_volume
            return
        if name in {"cycle_completed", "cycle_stopped"}:
            status = str(event_value(event, "status", ""))
            # Modern executions persist one leg_completed event per reconciled
            # fill batch.  Only use an aggregate cycle total to recover old
            # journals that genuinely have no leg-level evidence.
            if status not in {"completed", "recovered"} or self._completed_leg_quotes:
                return
            child_total = _nonnegative_decimal(event_value(event, "total_quote"))
            if child_total is not None:
                self.execution_verified_quote_volume = max(
                    self.execution_verified_quote_volume,
                    self._current_run_base_quote + child_total,
                )
            return
        if name in {"campaign_run_completed", "campaign_finished"}:
            total = _nonnegative_decimal(event_value(event, "total_quote"))
            if total is not None:
                self.execution_verified_quote_volume = max(self.execution_verified_quote_volume, total)
                self._current_run_base_quote = self.execution_verified_quote_volume
            return
        if name != "leg_completed":
            return
        quote = _nonnegative_decimal(event_value(event, "quote_volume"))
        if quote is None:
            return
        symbol = str(event_value(event, "symbol", "")).upper()
        round_number = event_value(event, "round", "")
        leg_sequence = event_value(event, "leg_sequence", event_value(event, "sequence", ""))
        action = str(event_value(event, "action", ""))
        key = f"{self.current_run}:{round_number}:{leg_sequence}:{symbol}:{action}"
        previous = self._completed_leg_quotes.get(key)
        if previous == quote:
            return
        # A leg_completed event is emitted only after the maker execution
        # service has reconciled actual fills for that leg.  Keep this
        # execution-journal total live while the independent fill ledger is
        # catching up; planned cycle values never enter this path.
        self.execution_verified_quote_volume += quote - (previous or Decimal(0))
        if symbol.startswith("BTC"):
            self.btc_quote_volume += quote - (previous or Decimal(0))
        elif symbol.startswith("ETH"):
            self.eth_quote_volume += quote - (previous or Decimal(0))
        self._completed_leg_quotes[key] = quote

    def _update_waits(self, event: Mapping[str, Any], at_ms: int) -> bool:
        name = event_name(event)
        round_number = event_value(event, "round", "")
        action = str(event_value(event, "action", ""))
        symbol = str(event_value(event, "symbol", "")) or None
        leg_sequence = event_value(event, "leg_sequence", event_value(event, "sequence", ""))
        leg_key = f"leg:{round_number}:{leg_sequence}:{symbol or ''}:{action}"
        if name != "campaign_read_retry":
            self.active_waits.pop("campaign-read-retry", None)

        if name in {"pair_waiting", "pair_wait_progress"}:
            active = event_value(event, "active_symbols", event_value(event, "symbols", ())) or ()
            symbols = "/".join(str(item) for item in active)
            self.active_waits.pop(f"cycle-stage:{round_number}", None)
            self._set_wait(
                ActiveWait(
                    key=f"pair:{round_number}:{action}",
                    label=f"{symbols} {action_label(action)} · 等待进入确定状态",
                    updated_at_ms=at_ms,
                    elapsed_ms=int(event_value(event, "elapsed_ms", 0) or 0),
                    remaining_ms=int(event_value(event, "remaining_ms", 0) or 0),
                    detail="到期后自动撤单并核验仓位",
                    action=action,
                )
            )
            return True

        if name == "leg_progress":
            progress = str(event_value(event, "progress_event", ""))
            if progress != "wait":
                self.active_waits.pop(leg_key, None)
                return False
            waiting_for = str(event_value(event, "waiting_for", ""))
            detail = ""
            if waiting_for == "maker_fill":
                detail = (
                    f"本单成交 {event_value(event, 'filled_quantity', '0')}/{event_value(event, 'order_quantity', '-')}"
                )
            waiting_label = WAITING_LABELS_ZH.get(waiting_for, waiting_for)
            self._set_wait(
                ActiveWait(
                    key=leg_key,
                    label=f"{symbol or ''} {action_label(action)} · {waiting_label}".strip(),
                    updated_at_ms=at_ms,
                    elapsed_ms=int(event_value(event, "elapsed_ms", 0) or 0),
                    remaining_ms=int(event_value(event, "remaining_ms", 0) or 0),
                    detail=detail,
                    symbol=symbol,
                    action=action,
                )
            )
            return True

        if name in {"leg_started", "leg_completed", "leg_stopped", "leg_uncertain"}:
            self.active_waits.pop(leg_key, None)
        elif name == "leg_preparing":
            self._set_wait(
                ActiveWait(
                    leg_key,
                    f"{symbol or ''} {action_label(action)} · 读取实际仓位".strip(),
                    at_ms,
                    symbol=symbol,
                    action=action,
                )
            )
            return True
        elif name == "leg_waiting":
            waiting_for = str(event_value(event, "waiting_for", ""))
            waiting_label = WAITING_LABELS_ZH.get(waiting_for, waiting_for)
            self._set_wait(
                ActiveWait(
                    leg_key,
                    f"{symbol or ''} {action_label(action)} · {waiting_label}".strip(),
                    at_ms,
                    symbol=symbol,
                    action=action,
                )
            )
            return True

        seconds = int(float(event_value(event, "seconds", 0) or 0) * 1000)
        finite = {
            "hold_started": ("hold", "双边持仓计时"),
            "round_gap_started": ("round-gap", "等待进入下一周期"),
            "preflight_retry": ("preflight", "执行前检查失败，等待重试"),
            "campaign_read_retry": ("campaign-read-retry", "Campaign 只读检查失败，等待重试"),
            "accounting_retry_wait": (f"accounting:{symbol or ''}", f"{symbol or ''} · 成交明细尚未完整，等待重查"),
        }
        if name in {"cycle_sizing_retry", "cycle_read_retry"}:
            if name == "cycle_sizing_retry":
                finite[name] = (f"cycle-read:{round_number}", "盘口读取失败，等待重新计算本轮数量")
            elif event_value(event, "read") == "balance":
                finite[name] = (f"cycle-read:{round_number}", "余额读取失败，等待重查")
            else:
                finite[name] = (f"cycle-read:{round_number}", "杠杆读取失败，等待重查")
        if name in finite:
            key, label = finite[name]
            self._set_wait(ActiveWait(key, label, at_ms, remaining_ms=seconds, symbol=symbol, action=action or None))
            return True

        stages = {
            "campaign_boundary_started": ("campaign-boundary", "读取账户持仓与挂单边界"),
            "campaign_child_planning_started": ("campaign-child-plan", "读取 Beta 与盘口并生成子计划"),
            "preflight_started": ("preflight", "检查 Beta、行情、资金、持仓和委托"),
            "cycle_preparing": (f"cycle-stage:{round_number}", "读取 BTC/ETH 盘口并计算本轮数量"),
            "leverage_preparing": (f"cycle-stage:{round_number}", "查询余额并配置本轮杠杆"),
            "close_barrier_started": (f"cycle-stage:{round_number}", "读取实际持仓并准备并发平仓"),
            "accounting_waiting": (f"accounting:{symbol or ''}", f"{symbol or ''} · 等待成交明细对账"),
            "final_acceptance_started": ("final-acceptance", "最终验收空仓、挂单、Maker 成交和交易量"),
            "safe_stop_started": ("safe-stop", "正在撤销 BTC/ETH 常规单与条件单"),
            "safe_stop_flattening": (f"safe-stop:{symbol or ''}", f"{symbol or ''} · 正在 Maker-only 平仓"),
        }
        if name in stages:
            key, label = stages[name]
            self._set_wait(ActiveWait(key, label, at_ms, symbol=symbol, action=action or None))
            return True

        removals = {
            "campaign_boundary_completed": ("campaign-boundary",),
            "campaign_child_planning_completed": ("campaign-child-plan",),
            "preflight_completed": ("preflight",),
            "preflight_rejected": ("preflight",),
            "cycle_started": (f"cycle-stage:{round_number}", f"cycle-read:{round_number}"),
            "cycle_completed": (f"cycle-stage:{round_number}", f"cycle-read:{round_number}"),
            "cycle_stopped": (f"cycle-stage:{round_number}", f"cycle-read:{round_number}"),
            "pair_wait_completed": (f"pair:{round_number}:{action}",),
            "hold_completed": ("hold",),
            "round_gap_completed": ("round-gap",),
            "accounting_wait_completed": (f"accounting:{symbol or ''}",),
            "final_acceptance_completed": ("final-acceptance",),
            "safe_stop_cancel_unverified": ("safe-stop", f"safe-stop:{symbol or ''}"),
            "safe_stop_uncertain": ("safe-stop", f"safe-stop:{symbol or ''}"),
            "safe_stop_verified": ("safe-stop", "safe-stop:BTC", "safe-stop:ETH"),
        }
        if name in {"workflow_finished", "campaign_finished"}:
            self.active_waits.clear()
        for key in removals.get(name, ()):
            self.active_waits.pop(key, None)
        if name == "pair_wait_completed":
            prefix = f"leg:{round_number}:"
            suffix = f":{action}"
            for key in tuple(self.active_waits):
                if key.startswith(prefix) and key.endswith(suffix):
                    self.active_waits.pop(key, None)
        return False

    def _update_counts(self, event: Mapping[str, Any]) -> None:
        if event_name(event) != "leg_progress":
            return
        progress = str(event_value(event, "progress_event", ""))
        if progress == "submit":
            self.submissions += 1
        elif progress == "cancel":
            self.cancels += 1
            self.requotes += 1


def _nonnegative_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except Exception:  # noqa: BLE001 - malformed observability values are ignored.
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _decimal_or_zero(value: Any) -> Decimal:
    return _nonnegative_decimal(value) or Decimal(0)


def _nonnegative_int(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)

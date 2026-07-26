"""Active-wait projection behaviour shared by the progress projector."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from .contracts import WAITING_LABELS_ZH, ActiveWait, action_label, condition_presentation, event_name, event_value
from .helpers import _nonnegative_int


class ExecutionProgressWaitMixin:
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

    def _clear_round_waits(self, round_number: Any) -> None:
        round_text = str(round_number)
        exact = {
            "hold",
            "round-gap",
            f"cycle-stage:{round_text}",
            f"cycle-read:{round_text}",
        }
        prefixes = (
            f"phase-pacing:{round_text}:",
            f"pair:{round_text}:",
            f"leg:{round_text}:",
            "accounting:",
        )
        for key in tuple(self.active_waits):
            if key in exact or key.startswith(prefixes):
                self.active_waits.pop(key, None)

    def _is_stale_round_wait(self, event: Mapping[str, Any]) -> bool:
        name = event_name(event)
        round_id = _nonnegative_int(event_value(event, "round", ""))
        creates_wait = (
            name in {"hold_started", "close_barrier_started", "accounting_waiting", "accounting_retry_wait"}
            or name in {"pair_waiting", "pair_wait_progress", "leg_preparing", "leg_waiting"}
            or (name == "leg_progress" and str(event_value(event, "progress_event", "")) == "wait")
        )
        return round_id in self._terminal_rounds and creates_wait

    def _update_waits(self, event: Mapping[str, Any], at_ms: int) -> bool:
        name = event_name(event)
        round_number = event_value(event, "round", "")
        round_id = _nonnegative_int(round_number)
        action = str(event_value(event, "action", ""))
        symbol = str(event_value(event, "symbol", "")) or None
        leg_sequence = event_value(event, "leg_sequence", event_value(event, "sequence", ""))
        leg_key = f"leg:{round_number}:{leg_sequence}:{symbol or ''}:{action}"
        if name != "campaign_read_retry":
            self.active_waits.pop("campaign-read-retry", None)

        if self._is_stale_round_wait(event):
            return False

        if name in {"cycle_completed", "cycle_stopped"}:
            if round_id > 0:
                self._terminal_rounds.add(round_id)
            self._clear_round_waits(round_number)
            return False
        if name == "cycle_started":
            self.condition_state = None
            self.condition_attempt = 0
            self.next_condition_check_at_ms = None
            self.active_waits.pop("condition", None)
            self.active_waits.pop("round-gap", None)
            self.active_waits.pop(f"cycle-stage:{round_number}", None)

        if name == "hold_completed":
            self.active_waits.pop("hold", None)
            self._set_wait(
                ActiveWait(
                    key=f"cycle-stage:{round_number}",
                    label="持仓计时结束，正在进入平仓阶段",
                    updated_at_ms=at_ms,
                    action="close",
                )
            )
            return False

        if name == "round_gap_completed":
            self.active_waits.pop("round-gap", None)
            return False

        if name == "condition_waiting":
            condition = str(event_value(event, "condition", ""))
            label, action_detail = condition_presentation(condition)
            deadline_at_ms = _nonnegative_int(event_value(event, "next_check_ms")) or None
            self.condition_state = condition or None
            self.condition_attempt = _nonnegative_int(
                event_value(event, "condition_attempt", event_value(event, "attempt"))
            )
            self.next_condition_check_at_ms = deadline_at_ms
            self._set_wait(
                ActiveWait(
                    key="condition",
                    label=label,
                    updated_at_ms=at_ms,
                    remaining_ms=None if deadline_at_ms is None else max(0, deadline_at_ms - at_ms),
                    detail=action_detail,
                    started_at_ms=at_ms,
                    deadline_at_ms=deadline_at_ms,
                )
            )
            return True
        if name == "condition_wait_resumed":
            self.condition_state = None
            self.condition_attempt = 0
            self.next_condition_check_at_ms = None
            self.active_waits.pop("condition", None)

        pacing_key = f"phase-pacing:{round_number}:{event_value(event, 'phase', '')}"
        if name == "phase_pacing_started":
            phase_name = str(event_value(event, "phase", ""))
            if phase_name == "close":
                self.active_waits.pop("hold", None)
                self.active_waits.pop(f"cycle-stage:{round_number}", None)
            elif phase_name == "open":
                self.active_waits.pop("round-gap", None)
            phase = action_label(event_value(event, "phase", ""))
            deadline_at_ms = int(event_value(event, "deadline_at_ms", at_ms) or at_ms)
            self._set_wait(
                ActiveWait(
                    key=pacing_key,
                    label=f"全局执行错峰 · {phase}",
                    updated_at_ms=at_ms,
                    remaining_ms=max(0, deadline_at_ms - at_ms),
                    detail="到达槽位后重新读取账户与行情边界",
                    action=str(event_value(event, "phase", "")) or None,
                    started_at_ms=at_ms,
                    deadline_at_ms=deadline_at_ms,
                )
            )
            return True
        if name in {"phase_pacing_completed", "phase_pacing_cancelled"}:
            self.active_waits.pop(pacing_key, None)

        if name in {"pair_waiting", "pair_wait_progress"}:
            active = event_value(event, "active_symbols", event_value(event, "symbols", ())) or ()
            symbols = "/".join(str(item) for item in active)
            self.active_waits.pop(f"cycle-stage:{round_number}", None)
            if action == "close":
                self.active_waits.pop("hold", None)
            self._set_wait(
                ActiveWait(
                    key=f"pair:{round_number}:{action}",
                    label=f"{symbols} {action_label(action)} · 等待进入确定状态",
                    updated_at_ms=at_ms,
                    elapsed_ms=int(event_value(event, "elapsed_ms", 0) or 0),
                    remaining_ms=(
                        None
                        if event_value(event, "remaining_ms") is None
                        else int(event_value(event, "remaining_ms", 0) or 0)
                    ),
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
            if name in {"hold_started", "round_gap_started"}:
                self._clear_round_waits(round_number)
            started_at_ms = event_value(event, "started_at_ms")
            deadline_at_ms = event_value(event, "deadline_at_ms")
            self._set_wait(
                ActiveWait(
                    key,
                    label,
                    at_ms,
                    remaining_ms=seconds,
                    symbol=symbol,
                    action=action or None,
                    started_at_ms=None if started_at_ms is None else int(started_at_ms),
                    deadline_at_ms=None if deadline_at_ms is None else int(deadline_at_ms),
                )
            )
            return True

        stages = {
            "campaign_boundary_started": ("campaign-boundary", "读取账户持仓与挂单边界"),
            "campaign_child_planning_started": ("campaign-child-plan", "读取 Beta 与盘口并生成子计划"),
            "preflight_started": ("preflight", "检查 Beta、行情、资金、持仓和委托"),
            "cycle_preparing": (f"cycle-stage:{round_number}", "读取 BTC/ETH 盘口并计算本轮数量"),
            "leverage_preparing": (f"cycle-stage:{round_number}", "查询余额并配置本轮杠杆"),
            "close_barrier_started": (f"cycle-stage:{round_number}", "读取实际持仓并准备并发平仓"),
            "accounting_waiting": (f"accounting:{symbol or ''}", f"{symbol or ''} · 等待成交明细对账"),
            "final_acceptance_started": ("final-acceptance", "最终验收空仓、挂单、流动性策略和交易量"),
            "safe_stop_started": ("safe-stop", "正在撤销 BTC/ETH 常规单与条件单"),
            "safe_stop_flattening": (f"safe-stop:{symbol or ''}", f"{symbol or ''} · 正在优先使用 Maker 平仓"),
            "dust_close_detected": (f"dust-close:{symbol or ''}", f"{symbol or ''} · 正在市价清除小额尾仓"),
            "market_close_intent_persisted": (
                f"dust-close:{symbol or ''}",
                f"{symbol or ''} · 正在市价清除小额尾仓",
            ),
            "market_close_accepted": (
                f"dust-close:{symbol or ''}",
                f"{symbol or ''} · 核验市价平仓结果",
            ),
        }
        if name in stages:
            key, label = stages[name]
            if name == "close_barrier_started":
                self.active_waits.pop("hold", None)
                self.active_waits.pop("round-gap", None)
                self.active_waits.pop(f"phase-pacing:{round_number}:close", None)
                self.active_waits.pop(f"pair:{round_number}:close", None)
            self._set_wait(ActiveWait(key, label, at_ms, symbol=symbol, action=action or None))
            return name not in {"dust_close_detected", "market_close_intent_persisted", "market_close_accepted"}

        removals = {
            "campaign_boundary_completed": ("campaign-boundary",),
            "campaign_child_planning_completed": ("campaign-child-plan",),
            "preflight_completed": ("preflight",),
            "preflight_rejected": ("preflight",),
            "cycle_started": (f"cycle-stage:{round_number}", f"cycle-read:{round_number}"),
            "pair_wait_completed": (f"pair:{round_number}:{action}",),
            "accounting_wait_completed": (f"accounting:{symbol or ''}",),
            "final_acceptance_completed": ("final-acceptance",),
            "safe_stop_cancel_unverified": ("safe-stop", f"safe-stop:{symbol or ''}"),
            "safe_stop_uncertain": ("safe-stop", f"safe-stop:{symbol or ''}"),
            "safe_stop_verified": ("safe-stop", "safe-stop:BTC", "safe-stop:ETH"),
            "market_close_verified": (f"dust-close:{symbol or ''}",),
            "market_close_uncertain": (f"dust-close:{symbol or ''}",),
        }
        if name in {"workflow_finished", "campaign_finished"}:
            self.active_waits.clear()
            self.condition_state = None
            self.condition_attempt = 0
            self.next_condition_check_at_ms = None
        for key in removals.get(name, ()):
            self.active_waits.pop(key, None)
        if name == "pair_wait_completed":
            prefix = f"leg:{round_number}:"
            suffix = f":{action}"
            for key in tuple(self.active_waits):
                if key.startswith(prefix) and key.endswith(suffix):
                    self.active_waits.pop(key, None)
        return False

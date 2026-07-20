from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from rich import box
from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from weex_cli.i18n import text, translate_field, translate_message, translate_value


def render_human(payload: Any, console: Console) -> bool:
    if isinstance(payload, list):
        _render_rows(payload, console)
        return True
    if not isinstance(payload, Mapping):
        return False
    if payload.get("view") == "status":
        _render_status(payload, console)
        return True
    if payload.get("view") == "activity" or ("summary" in payload and "start_datetime" in payload):
        _render_activity(payload, console)
        return True
    if payload.get("status") == "dry_run" and payload.get("confirm"):
        _render_dry_run(payload, console)
        return True
    if payload.get("kind") == "beta_volume_execution":
        _render_beta_volume_result(payload, console)
        return True
    if payload.get("kind") == "beta_volume_recovery":
        _render_beta_volume_recovery_result(payload, console)
        return True
    if payload.get("kind") == "beta_volume_campaign_execution":
        _render_beta_campaign_result(payload, console)
        return True
    if payload.get("kind") == "live_maker_volume_execution":
        _render_live_maker_volume_result(payload, console)
        return True
    if "rounds" in payload and "rounds_requested" in payload:
        _render_soak_result(payload, console)
        return True
    if payload.get("status") in {"completed", "failed", "uncertain"} and (
        "maker_only" in payload or "execution" in payload
    ):
        _render_maker_result(payload, console)
        return True
    if payload.get("view") == "message":
        style = "green" if payload.get("status") in {"ok", "completed"} else "yellow"
        console.print(Panel(translate_message(payload.get("message") or ""), border_style=style))
        return True
    return False


def render_execution_event(event: Mapping[str, Any], console: Console) -> None:
    name = str(event.get("event") or "")
    if name == "campaign_run_started":
        console.print(
            f"[cyan]{text('Campaign 运行', 'Campaign run')} {event.get('run')}[/cyan]  "
            f"{text('剩余', 'remaining')} {event.get('remaining_quote')} USDT / "
            f"{text('子计划', 'child')} {event.get('child_plan_id')}"
        )
        return
    if name in {"campaign_boundary_started", "campaign_boundary_completed"}:
        started = name == "campaign_boundary_started"
        phase = str(event.get("phase") or "")
        phase_label = text("启动前", "before start") if phase == "initial" else text("周期检查点", "cycle checkpoint")
        label = (
            text("正在读取账户边界", "Reading account boundary")
            if started
            else text("账户边界读取完成", "Account boundary read complete")
        )
        style = "cyan" if started else "green"
        console.print(f"[{style}]{label}[/{style}]  {phase_label}")
        return
    if name in {"campaign_child_planning_started", "campaign_child_planning_completed"}:
        started = name == "campaign_child_planning_started"
        label = (
            text("正在读取 Beta 与盘口并生成本次子计划", "Reading Beta and books; planning child run")
            if started
            else text("子计划生成完成", "Child plan ready")
        )
        style = "cyan" if started else "green"
        detail = (
            f"{text('剩余', 'remaining')} {event.get('remaining_quote')} USDT"
            if started
            else f"{text('子计划', 'child')} {event.get('child_plan_id')}"
        )
        console.print(f"[{style}]{label}[/{style}]  {text('运行', 'run')} {event.get('run')} / {detail}")
        return
    if name == "campaign_run_completed":
        console.print(
            f"[green]{text('Campaign 运行', 'Campaign run')} {event.get('run')} "
            f"{text('已保存检查点', 'checkpointed')}[/green]  "
            f"{event.get('child_quote')} USDT / {text('累计', 'total')} {event.get('total_quote')} USDT / "
            f"{_display(event.get('child_status'))}"
        )
        return
    if name == "campaign_read_retry":
        operation = _display(event.get("operation"))
        child = f" / {text('子计划', 'child')} {event.get('child_plan_id')}" if event.get("child_plan_id") else ""
        console.print(
            f"[yellow]{text('Campaign 读取重试', 'Campaign read retry')} {event.get('attempt')}[/yellow]  "
            f"{operation}{child} / "
            f"{text('等待', 'waiting')} {event.get('seconds')}s"
        )
        return
    if name == "campaign_finished":
        style = "green" if event.get("status") == "completed" else "yellow"
        console.print(
            f"[{style}]Campaign {_display(event.get('status'))}[/{style}]  "
            f"{event.get('total_quote')} USDT / {translate_message(event.get('reason'))}"
        )
        return
    if name in {"hold_started", "hold_completed"}:
        label = (
            text("双边持仓等待", "Holding open pair")
            if name == "hold_started"
            else text("双边持仓等待完成", "Open-pair hold complete")
        )
        console.print(f"[cyan]{label}[/cyan]  {text('周期', 'cycle')} {event.get('round')} / {event.get('seconds')}s")
        return
    if name in {"round_gap_started", "round_gap_completed"}:
        label = (
            text("周期间隔", "Round gap") if name == "round_gap_started" else text("周期间隔完成", "Round gap complete")
        )
        console.print(
            f"[cyan]{label}[/cyan]  {text('周期', 'after cycle')} {event.get('round')} / {event.get('seconds')}s"
        )
        return
    if name == "preflight_started":
        console.print(
            f"[cyan]{text('执行前检查', 'Preflight')}[/cyan]  "
            + text("检查 Beta、行情、资金、持仓和委托", "Checking Beta, market, funds, positions, and orders")
        )
        return
    if name == "preflight_completed":
        console.print(
            f"[green]{text('执行前检查完成', 'Preflight complete')}[/green]  "
            + text("账户已就绪并确认空仓", "Account is ready and flat")
        )
        return
    if name == "preflight_rejected":
        console.print(
            f"[red]{text('执行前检查未通过', 'Preflight rejected')}[/red]  {translate_message(event.get('reason'))}"
        )
        return
    if name == "preflight_retry":
        console.print(
            f"[yellow]{text('执行前检查读取失败，等待重试', 'Preflight read failed; waiting to retry')}[/yellow]  "
            f"{text('第', 'attempt')} {event.get('attempt')} {text('次', '')} / {event.get('seconds')}s / "
            f"{translate_message(event.get('error') or event.get('reason'))}"
        )
        return
    if name == "cycle_started":
        console.print(
            f"[cyan]{text('周期', 'Cycle')} {event.get('round')}[/cyan]  "
            f"{text('目标', 'target')} {event.get('desired_quote')} USDT / "
            f"BTC {event.get('btc_quantity')} + ETH {event.get('eth_quantity')} / {event.get('leverage')}x"
        )
        return
    if name == "cycle_preparing":
        label = text(
            "正在读取 BTC/ETH 最新盘口并计算本周期数量",
            "Reading BTC/ETH books and sizing cycle",
        )
        console.print(
            f"[cyan]{label}[/cyan]  "
            f"{text('周期', 'cycle')} {event.get('round')} / {text('目标', 'target')} {event.get('desired_quote')} USDT"
        )
        return
    if name == "leverage_preparing":
        label = text(
            "正在查询余额与杠杆，必要时配置双币种杠杆",
            "Checking funds and configuring leverage if needed",
        )
        console.print(
            f"[cyan]{label}[/cyan]  "
            f"{text('周期', 'cycle')} {event.get('round')} / "
            f"{text('开仓名义金额', 'opening notional')} {event.get('opening_notional_quote')} USDT"
        )
        return
    if name in {"cycle_sizing_retry", "cycle_read_retry"}:
        if name == "cycle_sizing_retry":
            label = text("盘口读取失败，等待重新计算本周期数量", "Book read failed; waiting to resize cycle")
        elif event.get("read") == "balance":
            label = text("余额读取失败，等待重新查询", "Balance read failed; waiting to retry")
        else:
            label = text("杠杆状态读取失败，等待重新查询", "Leverage read failed; waiting to retry")
        symbol = f" / {event.get('symbol')}" if event.get("symbol") else ""
        console.print(
            f"[yellow]{label}[/yellow]{symbol} / "
            f"{text('尝试', 'attempt')} {event.get('attempt')}/{event.get('max_attempts')} / "
            f"{event.get('seconds')}s"
        )
        return
    if name == "leg_preparing":
        label = text("正在读取实际仓位", "Reading current position")
        console.print(
            f"[cyan][r{event.get('round')} #{event.get('sequence')}] {label}[/cyan]  "
            f"{event.get('symbol')} {_display(event.get('action'))}"
        )
        return
    if name == "leg_started":
        console.print(
            f"[cyan][r{event.get('round')} #{event.get('sequence')}][/cyan] "
            f"{text('准备', 'Preparing ')}{_display(event.get('action'))} {event.get('symbol')} "
            f"{_display(event.get('side'))} {event.get('quantity')}  [dim]POST_ONLY[/dim]"
        )
        return
    if name == "leg_progress":
        _render_leg_progress(event, console)
        return
    if name == "leg_waiting":
        labels = {
            "order_identity": text("确认成交订单身份", "confirming filled-order identity"),
            "fill_reconciliation": text(
                "等待 WEEX 成交明细并核对 Maker 交易量", "waiting for WEEX fills and Maker reconciliation"
            ),
            "open_order_clearance": text("确认该币种已无残留挂单", "confirming no open order remains"),
            "position_observation_retry": text(
                "仓位读取超时，等待重新查询",
                "position read timed out; waiting to retry",
            ),
            "order_observation_retry": text(
                "委托读取超时，等待重新查询",
                "order read timed out; waiting to retry",
            ),
        }
        waiting_for = str(event.get("waiting_for") or "")
        console.print(
            f"[cyan][r{event.get('round')} #{event.get('sequence')}] {text('正在等待', 'Waiting')}[/cyan]  "
            f"{event.get('symbol')} {_display(event.get('action'))} / "
            f"{labels.get(waiting_for, _display(waiting_for))}"
        )
        return
    if name == "position_observation_unavailable":
        label = text(
            "仓位连续读取失败，停止该通道继续下单",
            "Position remained unavailable; stopping this lane",
        )
        console.print(
            f"[red][r{event.get('round')} #{event.get('sequence')}] "
            f"{label}[/red]  "
            f"{event.get('symbol')} {_display(event.get('action'))}"
        )
        return
    if name in {"pair_waiting", "pair_wait_progress"}:
        symbols = "/".join(str(value) for value in (event.get("active_symbols") or event.get("symbols") or ()))
        elapsed = _duration(float(event.get("elapsed_ms") or 0), milliseconds=True)
        remaining = _duration(float(event.get("remaining_ms") or 0), milliseconds=True)
        console.print(
            f"[cyan]{text('双腿屏障：仍在等待', 'Pair barrier: still waiting for')} {symbols} "
            f"{_display(event.get('action'))} {text('进入确定状态', 'to reach a determinate state')}[/cyan]  "
            f"{text('已等待', 'elapsed')} {elapsed} / {text('剩余硬截止', 'hard deadline remaining')} {remaining} / "
            f"{text('到期后自动撤单并核验仓位', 'then cancel and verify the position')}"
        )
        return
    if name == "pair_wait_completed":
        console.print(
            f"[green]{text('双腿屏障已通过', 'Pair barrier passed')}[/green]  "
            f"{text('周期', 'cycle')} {event.get('round')} / {_display(event.get('action'))}"
        )
        return
    if name == "close_barrier_started":
        label = text(
            "开仓阶段已结束，正在读取实际持仓并准备并发平仓",
            "Open phase ended; reading positions and preparing concurrent close",
        )
        console.print(f"[cyan]{label}[/cyan]  {text('周期', 'cycle')} {event.get('round')}")
        return
    if name == "accounting_waiting":
        console.print(
            f"[cyan]{text('正在等待成交明细对账', 'Waiting for fill reconciliation')}[/cyan]  "
            f"{event.get('symbol')} {_display(event.get('action'))} / "
            f"{text('尝试', 'attempt')} {event.get('attempt')}/{event.get('max_attempts')}"
        )
        return
    if name == "accounting_retry_wait":
        console.print(
            f"[yellow]{text('成交明细尚未完整，等待后重查', 'Fills not complete; waiting to retry')}[/yellow]  "
            f"{event.get('symbol')} / {event.get('seconds')}s / "
            f"{text('下一次', 'next')} {event.get('attempt')}/{event.get('max_attempts')}"
        )
        return
    if name == "accounting_wait_completed":
        console.print(
            f"[green]{text('成交明细对账完成', 'Fill reconciliation complete')}[/green]  "
            f"{event.get('symbol')} / {_display(event.get('status'))}"
        )
        return
    if name == "final_acceptance_started":
        checks = text(
            "核对空仓、挂单、Maker 成交与累计交易量",
            "checking flat positions, orders, Maker fills, and volume",
        )
        console.print(
            f"[cyan]{text('正在执行最终验收', 'Running final acceptance')}[/cyan]  "
            f"{checks} / {event.get('total_quote')} USDT"
        )
        return
    if name == "final_acceptance_completed":
        style = "green" if event.get("completed") else "yellow"
        console.print(
            f"[{style}]{text('最终验收完成', 'Final acceptance complete')}[/{style}]  "
            f"{text('空仓', 'flat')}={_yes_no(event.get('flat'))} / "
            f"{text('无挂单', 'no orders')}={_yes_no(event.get('no_orders'))} / "
            f"Maker={_yes_no(event.get('maker_only'))}"
        )
        return
    if name == "leg_completed":
        console.print(
            f"[green][r{event.get('round')} #{event.get('sequence')}] {text('已验证', 'verified')}[/green] "
            f"{event.get('quote_volume')} USDT / {event.get('fill_count')} {text('笔成交', 'fill(s)')} / "
            f"{_duration(event.get('elapsed_ms'), milliseconds=True)}"
        )
        return
    if name in {"leg_stopped", "leg_uncertain"}:
        style = "red" if name == "leg_stopped" else "yellow"
        console.print(
            f"[{style}][#{event.get('sequence')}] "
            f"{text('已停止', 'stopped') if name == 'leg_stopped' else text('状态不确定', 'uncertain')}[/{style}] "
            f"{event.get('symbol')} {_display(event.get('action'))}: {translate_message(event.get('reason'))}"
        )
        return
    if name in {"cycle_completed", "cycle_stopped"}:
        style = "green" if name == "cycle_completed" else "yellow"
        console.print(
            f"[{style}]{text('周期', 'Cycle')} {event.get('round')} {_display(event.get('status'))}[/{style}]  "
            f"{event.get('quote_volume')} USDT / {text('累计', 'total')} {event.get('total_quote')} USDT / "
            f"{translate_message(event.get('reason'))}"
        )
        return
    if name == "workflow_finished":
        style = "green" if event.get("status") == "completed" else "yellow"
        console.print(
            f"[{style}]{text('流程', 'Workflow')} {_display(event.get('status'))}[/{style}]  "
            f"{event.get('executed_quote_volume')} USDT / {translate_message(event.get('reason'))}"
        )


def _render_leg_progress(event: Mapping[str, Any], console: Console) -> None:
    progress = str(event.get("progress_event") or "")
    prefix = f"[r{event.get('round')} #{event.get('sequence')}] {event.get('symbol')} {_display(event.get('action'))}"
    if progress == "market_data_source":
        source = str(event.get("source") or "rest")
        if source == "websocket":
            label = text("盘口已切换到 WebSocket 实时深度", "market data switched to live WebSocket depth")
            style = "green"
        else:
            label = text("WebSocket 盘口不可用，已安全回退 REST", "WebSocket book unavailable; using REST safely")
            style = "yellow"
        console.print(f"[{style}]{prefix} {label}[/{style}]")
        return
    if progress == "submit":
        console.print(
            f"[cyan]{prefix} {text('Maker 挂单已提交', 'Maker order submitted')}[/cyan]  "
            f"{text('价格', 'price')} {event.get('price')} / {text('数量', 'quantity')} {event.get('quantity')}"
        )
        return
    if progress == "fill":
        console.print(
            f"[green]{prefix} {text('观察到 Maker 成交', 'Maker fill observed')}[/green]  "
            f"{text('数量', 'quantity')} {event.get('quantity')} / {event.get('quote')} USDT"
        )
        return
    if progress == "cancel_started":
        label = text(
            "报价需要更新，正在撤单并确认结果",
            "quote needs update; canceling and verifying",
        )
        console.print(f"[yellow]{prefix} {label}[/yellow]")
        return
    if progress == "cancel":
        console.print(
            f"[green]{prefix} {text('撤单已确认，准备重新报价', 'cancel confirmed; preparing requote')}[/green]  "
            f"{translate_message(event.get('reason'))}"
        )
        return
    if progress == "timeout_cleanup_started":
        label = text(
            "已达到腿超时，正在取消普通单和条件单",
            "leg deadline reached; canceling regular and conditional orders",
        )
        console.print(f"[yellow]{prefix} {label}[/yellow]")
        return
    if progress == "timeout_cleanup_confirmed":
        label = text(
            "超时清理已确认，允许读取残仓并进行 Maker 平仓",
            "timeout cleanup confirmed; residual Maker flattening is allowed",
        )
        console.print(f"[green]{prefix} {label}[/green]")
        return
    if progress in {"timeout_cleanup_not_confirmed", "timeout_cleanup_error"}:
        label = text(
            "超时清理未能确认，禁止继续下单",
            "timeout cleanup was not confirmed; no further orders allowed",
        )
        console.print(f"[red]{prefix} {label}[/red]")
        return
    if progress == "timeout_order_not_confirmed":
        label = text(
            "超时订单状态未能确认，进入不确定状态",
            "timed-out order state was not confirmed; entering uncertain state",
        )
        console.print(f"[red]{prefix} {label}[/red]")
        return
    if progress == "order_terminal":
        label = text(
            "挂单已进入终态，正在核对目标仓位",
            "order terminal; checking target position",
        )
        console.print(f"[cyan]{prefix} {label}[/cyan]  {_display(event.get('status'))}")
        return
    if progress != "wait":
        return

    waiting_for = str(event.get("waiting_for") or "")
    labels = {
        "maker_fill": text("等待 Maker 挂单成交", "waiting for Maker fill"),
        "cancel_confirmation": text("等待撤单最终状态", "waiting for final cancel state"),
        "order_observation_retry": text("订单状态读取失败，等待重试", "order read failed; waiting to retry"),
        "position_observation_retry": text("仓位读取超时，等待重新查询", "position read timed out; waiting to retry"),
        "market_observation_retry": text("盘口读取超时，等待重新查询", "market read timed out; waiting to retry"),
        "submission_slot": text("等待下单限频窗口", "waiting for submission slot"),
        "submission_preflight_retry": text("盘口已变化，等待重新计算 Maker 报价", "book changed; waiting to reprice"),
        "submission_recovery": text("下单响应不确定，按客户订单号查询", "submission uncertain; checking client ID"),
        "submission_verification": text("下单后状态读取失败，等待重新验证", "post-submit read failed; retrying"),
        "submission_post_only_verification": text("等待确认订单保持 POST_ONLY", "waiting to verify POST_ONLY"),
        "submission_book_check": text("下单前盘口读取失败，等待重查", "pre-submit book read failed; retrying"),
        "amount_precision": text("数量精度读取失败，等待重查", "amount precision read failed; retrying"),
        "price_precision": text("价格精度读取失败，等待重查", "price precision read failed; retrying"),
        "cleanup_order_observation": text("清理后委托读取失败，等待重查", "cleanup order read failed; retrying"),
        "cleanup_order_clearance": text("清理后委托仍可见，等待消失", "waiting for canceled orders to disappear"),
        "precheck_positions": text("下单前仓位读取失败，等待重查", "precheck position read failed; retrying"),
        "precheck_open_orders": text("下单前委托读取失败，等待重查", "precheck order read failed; retrying"),
    }
    details: list[str] = [
        labels.get(waiting_for, _display(waiting_for)),
        f"{text('已等待', 'elapsed')} {_duration(event.get('elapsed_ms'), milliseconds=True)}",
        f"{text('剩余超时', 'timeout left')} {_duration(event.get('remaining_ms'), milliseconds=True)}",
    ]
    if waiting_for == "maker_fill":
        details.append(f"{text('本单成交', 'order fill')} {event.get('filled_quantity')}/{event.get('order_quantity')}")
    if event.get("attempt") is not None:
        details.append(f"{text('尝试', 'attempt')} {event.get('attempt')}/{event.get('max_attempts')}")
    console.print(f"[cyan]{prefix} {text('正在等待', 'Waiting')}[/cyan]  " + " / ".join(details))


@dataclass
class _ActiveWait:
    label: str
    elapsed_seconds: float
    remaining_seconds: float | None
    updated_at: float
    detail: str = ""


class TerminalExecutionProgress:
    """Render concurrent execution waits as one transient, live terminal area."""

    _FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(
        self,
        console: Console,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        interactive: bool | None = None,
        auto_refresh: bool = True,
    ) -> None:
        self.console = console
        self.monotonic = monotonic
        self.interactive = console.is_terminal if interactive is None else interactive
        self._lock = threading.RLock()
        self._live_lifecycle_lock = threading.Lock()
        self._waits: dict[str, _ActiveWait] = {}
        self._live = Live(
            self,
            console=console,
            refresh_per_second=8,
            transient=True,
            auto_refresh=auto_refresh,
        )
        self._live_started = False

    def __call__(self, event: Mapping[str, Any]) -> None:
        if not self.interactive:
            render_execution_event(event, self.console)
            return
        with self._lock:
            consumed = self._update_waits(event)
        self._sync_live()
        if not consumed:
            render_execution_event(event, self.console)

    def close(self) -> None:
        if not self.interactive:
            return
        with self._live_lifecycle_lock:
            with self._lock:
                self._waits.clear()
                should_stop = self._live_started
                self._live_started = False
            if should_stop:
                self._live.stop()

    def refresh(self) -> None:
        if not self.interactive:
            return
        with self._live_lifecycle_lock:
            with self._lock:
                should_refresh = self._live_started
            if should_refresh:
                self._live.refresh()

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        with self._lock:
            waits = tuple(self._waits.values())
        now = self.monotonic()
        frame = self._FRAMES[int(now * 8) % len(self._FRAMES)]
        for wait in waits:
            delta = max(0.0, now - wait.updated_at)
            elapsed = wait.elapsed_seconds + delta
            remaining = max(0.0, wait.remaining_seconds - delta) if wait.remaining_seconds is not None else None
            line = Text()
            line.append(f"{frame} ", style="bold cyan")
            line.append(wait.label, style="cyan")
            line.append(
                f"  {text('已等待', 'elapsed')} {_duration(elapsed * 1000, milliseconds=True)}",
                style="dim",
            )
            if remaining is not None:
                line.append(
                    f" / {text('剩余', 'remaining')} {_duration(remaining * 1000, milliseconds=True)}",
                    style="dim",
                )
            if wait.detail:
                line.append(f" / {wait.detail}", style="dim")
            yield line

    def _sync_live(self) -> None:
        with self._live_lifecycle_lock:
            with self._lock:
                has_waits = bool(self._waits)
                was_started = self._live_started
                if has_waits and not was_started:
                    self._live_started = True
                elif not has_waits and was_started:
                    self._live_started = False
            if has_waits:
                if not was_started:
                    self._live.start(refresh=True)
                else:
                    self._live.update(self, refresh=True)
            elif was_started:
                self._live.stop()

    def _set_wait(
        self,
        key: str,
        label: str,
        *,
        elapsed_seconds: float = 0.0,
        remaining_seconds: float | None = None,
        detail: str = "",
    ) -> None:
        self._waits[key] = _ActiveWait(
            label=label,
            elapsed_seconds=max(0.0, elapsed_seconds),
            remaining_seconds=max(0.0, remaining_seconds) if remaining_seconds is not None else None,
            updated_at=self.monotonic(),
            detail=detail,
        )

    def _update_waits(self, event: Mapping[str, Any]) -> bool:
        name = str(event.get("event") or "")
        leg_key = self._leg_key(event)
        if name != "campaign_read_retry":
            self._waits.pop("campaign-read-retry", None)

        if name in {"pair_waiting", "pair_wait_progress"}:
            round_number = event.get("round")
            action = event.get("action")
            active = tuple(event.get("active_symbols") or event.get("symbols") or ())
            symbols = "/".join(str(symbol) for symbol in active)
            self._waits.pop(f"cycle-stage:{round_number}", None)
            self._set_wait(
                f"pair:{round_number}:{action}",
                f"{symbols} {_display(action)} · {text('等待进入确定状态', 'Waiting for a determinate state')}",
                elapsed_seconds=float(event.get("elapsed_ms") or 0) / 1000,
                remaining_seconds=float(event.get("remaining_ms") or 0) / 1000,
                detail=text(
                    "到期后自动撤单并核验仓位",
                    "then cancel and verify the position",
                ),
            )
            return True

        if name == "leg_progress":
            progress = str(event.get("progress_event") or "")
            if progress != "wait":
                self._waits.pop(leg_key, None)
                return False
            waiting_for = str(event.get("waiting_for") or "")
            labels = {
                "maker_fill": text("等待 Maker 挂单成交", "Waiting for Maker fill"),
                "cancel_confirmation": text("等待撤单最终状态", "Waiting for final cancel state"),
                "order_observation_retry": text("等待重新读取订单状态", "Waiting to retry order read"),
                "position_observation_retry": text("等待重新读取仓位", "Waiting to retry position read"),
                "market_observation_retry": text("等待重新读取盘口", "Waiting to retry market read"),
                "submission_slot": text("等待下单限频窗口", "Waiting for submission slot"),
                "submission_preflight_retry": text("等待重新计算 Maker 报价", "Waiting to reprice Maker order"),
                "submission_recovery": text("按客户订单号确认下单结果", "Checking submission by client ID"),
                "submission_verification": text("重新验证下单状态", "Retrying post-submit verification"),
                "submission_post_only_verification": text("确认订单保持 POST_ONLY", "Verifying POST_ONLY"),
                "submission_book_check": text("重新读取下单前盘口", "Retrying pre-submit book read"),
                "amount_precision": text("重新读取数量精度", "Retrying amount precision read"),
                "price_precision": text("重新读取价格精度", "Retrying price precision read"),
                "cleanup_order_observation": text("重新读取清理后的委托", "Retrying cleanup order read"),
                "cleanup_order_clearance": text("等待清理后的委托消失", "Waiting for cleanup clearance"),
                "precheck_positions": text("重新读取下单前仓位", "Retrying precheck position read"),
                "precheck_open_orders": text("重新读取下单前委托", "Retrying precheck order read"),
            }
            detail = ""
            if waiting_for == "maker_fill":
                detail = (
                    f"{text('本单成交', 'order fill')} {event.get('filled_quantity')}/{event.get('order_quantity')}"
                )
            self._set_wait(
                leg_key,
                f"{event.get('symbol')} {_display(event.get('action'))} · "
                f"{labels.get(waiting_for, _display(waiting_for))}",
                elapsed_seconds=float(event.get("elapsed_ms") or 0) / 1000,
                remaining_seconds=float(event.get("remaining_ms") or 0) / 1000,
                detail=detail,
            )
            return True

        if name in {"leg_started", "leg_completed", "leg_stopped", "leg_uncertain"}:
            self._waits.pop(leg_key, None)
            return False
        if name == "leg_preparing":
            self._set_wait(
                leg_key,
                f"{event.get('symbol')} {_display(event.get('action'))} · "
                f"{text('读取实际仓位', 'Reading current position')}",
            )
            return True
        if name == "leg_waiting":
            labels = {
                "order_identity": text("确认成交订单身份", "Confirming filled-order identity"),
                "fill_reconciliation": text("等待 WEEX 成交明细对账", "Waiting for WEEX fill reconciliation"),
                "open_order_clearance": text("确认无残留挂单", "Confirming no open order remains"),
                "position_observation_retry": text("仓位读取超时，等待重新查询", "Waiting to retry position read"),
                "order_observation_retry": text("委托读取超时，等待重新查询", "Waiting to retry order read"),
            }
            waiting_for = str(event.get("waiting_for") or "")
            self._set_wait(
                leg_key,
                f"{event.get('symbol')} {_display(event.get('action'))} · "
                f"{labels.get(waiting_for, _display(waiting_for))}",
            )
            return True

        finite = self._finite_wait(event)
        if finite is not None:
            key, label, seconds = finite
            self._set_wait(key, label, remaining_seconds=seconds)
            return True

        start = self._stage_wait(event)
        if start is not None:
            key, label = start
            self._set_wait(key, label)
            return True

        self._clear_completed_wait(event)
        return False

    def _finite_wait(self, event: Mapping[str, Any]) -> tuple[str, str, float] | None:
        name = str(event.get("event") or "")
        seconds = float(event.get("seconds") or 0)
        if name == "hold_started":
            return "hold", text("双边持仓计时", "Holding open pair"), seconds
        if name == "round_gap_started":
            return "round-gap", text("等待进入下一周期", "Waiting for next cycle"), seconds
        if name == "preflight_retry":
            return "preflight", text("执行前检查失败，等待重试", "Preflight failed; waiting to retry"), seconds
        if name == "campaign_read_retry":
            return (
                "campaign-read-retry",
                text("Campaign 只读检查失败，等待重试", "Campaign read failed; waiting to retry"),
                seconds,
            )
        if name in {"cycle_sizing_retry", "cycle_read_retry"}:
            if name == "cycle_sizing_retry":
                label = text("盘口读取失败，等待重新计算周期数量", "Book read failed; waiting to resize cycle")
            elif event.get("read") == "balance":
                label = text("余额读取失败，等待重查", "Balance read failed; waiting to retry")
            else:
                label = text("杠杆读取失败，等待重查", "Leverage read failed; waiting to retry")
            return (f"cycle-read:{event.get('round') or ''}", label, seconds)
        if name == "accounting_retry_wait":
            return (
                f"accounting:{event.get('symbol')}",
                f"{event.get('symbol')} · {text('成交明细尚未完整，等待重查', 'Waiting to retry fill reconciliation')}",
                seconds,
            )
        return None

    def _stage_wait(self, event: Mapping[str, Any]) -> tuple[str, str] | None:
        name = str(event.get("event") or "")
        round_number = event.get("round")
        stages = {
            "campaign_boundary_started": (
                "campaign-boundary",
                text("读取账户持仓与挂单边界", "Reading account positions and orders"),
            ),
            "campaign_child_planning_started": (
                "campaign-child-plan",
                text("读取 Beta 与盘口并生成子计划", "Reading Beta and books; planning child run"),
            ),
            "preflight_started": (
                "preflight",
                text("执行前检查 Beta、行情、资金、持仓和委托", "Checking Beta, market, funds, positions, and orders"),
            ),
            "cycle_preparing": (
                f"cycle-stage:{round_number}",
                text("读取 BTC/ETH 盘口并计算本周期数量", "Reading BTC/ETH books and sizing cycle"),
            ),
            "leverage_preparing": (
                f"cycle-stage:{round_number}",
                text("查询余额并配置本周期杠杆", "Checking funds and configuring cycle leverage"),
            ),
            "close_barrier_started": (
                f"cycle-stage:{round_number}",
                text("读取实际持仓并准备并发平仓", "Reading positions and preparing concurrent close"),
            ),
            "pair_waiting": (
                f"pair:{round_number}:{event.get('action')}",
                f"BTC/ETH {_display(event.get('action'))} · "
                f"{text('等待双腿进入确定状态', 'Waiting for both legs to become determinate')}",
            ),
            "accounting_waiting": (
                f"accounting:{event.get('symbol')}",
                f"{event.get('symbol')} · {text('等待成交明细对账', 'Waiting for fill reconciliation')}",
            ),
            "final_acceptance_started": (
                "final-acceptance",
                text("最终验收空仓、挂单、Maker 成交和交易量", "Final acceptance checks"),
            ),
        }
        return stages.get(name)

    def _clear_completed_wait(self, event: Mapping[str, Any]) -> None:
        name = str(event.get("event") or "")
        round_number = event.get("round")
        removals = {
            "campaign_boundary_completed": ("campaign-boundary",),
            "campaign_child_planning_completed": ("campaign-child-plan",),
            "preflight_completed": ("preflight",),
            "preflight_rejected": ("preflight",),
            "cycle_started": (f"cycle-stage:{round_number}", f"cycle-read:{round_number}"),
            "cycle_completed": (f"cycle-stage:{round_number}", f"cycle-read:{round_number}"),
            "cycle_stopped": (f"cycle-stage:{round_number}", f"cycle-read:{round_number}"),
            "pair_wait_completed": (f"pair:{round_number}:{event.get('action')}",),
            "hold_completed": ("hold",),
            "round_gap_completed": ("round-gap",),
            "accounting_wait_completed": (f"accounting:{event.get('symbol')}",),
            "final_acceptance_completed": ("final-acceptance",),
            "workflow_finished": tuple(self._waits),
            "campaign_finished": tuple(self._waits),
        }
        for key in removals.get(name, ()):
            self._waits.pop(key, None)
        if name == "pair_wait_completed":
            prefix = f"leg:{round_number}:"
            suffix = f":{event.get('action')}"
            for key in tuple(self._waits):
                if key.startswith(prefix) and key.endswith(suffix):
                    self._waits.pop(key, None)

    @staticmethod
    def _leg_key(event: Mapping[str, Any]) -> str:
        return ":".join(
            (
                "leg",
                str(event.get("round") or ""),
                str(event.get("sequence") or ""),
                str(event.get("symbol") or ""),
                str(event.get("action") or ""),
            )
        )


def render_live_volume_event(event: Mapping[str, Any], console: Console) -> None:
    name = str(event.get("event") or "")
    if name == "volume_preflight_started":
        console.print(
            f"[cyan]{text('执行前检查', 'Preflight')}[/cyan]  "
            f"{text('检查', 'Checking')} {event.get('symbol')} "
            f"{text('资金、持仓和委托', 'funds, positions, and orders')}"
        )
        return
    if name == "volume_preflight_completed":
        console.print(
            f"[green]{text('执行前检查完成', 'Preflight complete')}[/green]  "
            + text("账户已就绪并确认空仓", "Account is ready and flat")
        )
        return
    if name == "volume_preflight_rejected":
        console.print(
            f"[red]{text('执行前检查未通过', 'Preflight rejected')}[/red]  {translate_message(event.get('reason'))}"
        )
        return
    if name == "volume_round_started":
        console.print(
            f"[cyan]{text('轮次', 'Round')} {event.get('round')}[/cyan]  {_display(event.get('position_side'))} "
            f"{event.get('quantity')} / {text('目标', 'target')} {event.get('desired_quote')} USDT"
        )
        return
    if name == "volume_leg_started":
        console.print(
            f"  [dim]{_display(event.get('action'))} {text('尝试', 'attempt')} {event.get('attempt')}[/dim]  "
            f"{_display(event.get('side'))} -> {event.get('target_position')}  POST_ONLY"
        )
        return
    if name in {"volume_leg_completed", "volume_leg_stopped"}:
        style = "green" if name == "volume_leg_completed" else "yellow"
        console.print(
            f"  [{style}]{_display(event.get('action'))} {_display(event.get('status'))}[/{style}]  "
            f"{event.get('quote_volume')} USDT / {text('累计', 'total')} {event.get('total_verified_quote')} / "
            f"{translate_message(event.get('reason'))}"
        )
        return
    if name == "volume_cooldown":
        console.print(
            f"[dim]{text('本轮已确认空仓。剩余', 'Round flat. Remaining')} "
            f"{event.get('remaining_quote')} USDT；{text('等待', 'cooldown')} {event.get('seconds')}s[/dim]"
        )
        return
    if name == "volume_workflow_finished":
        style = "green" if event.get("status") == "completed" else "yellow"
        console.print(
            f"[{style}]{text('交易量会话', 'Volume session')} {_display(event.get('status'))}[/{style}]  "
            f"{event.get('verified_quote')} USDT / {translate_message(event.get('reason'))}"
        )


def _render_status(payload: Mapping[str, Any], console: Console) -> None:
    mode = str(payload.get("mode") or "unknown").upper()
    symbol = str(payload.get("symbol") or "all").upper()
    position = _mapping(payload.get("position"))
    orders = _mapping(payload.get("orders"))
    credentials = _mapping(payload.get("credentials"))

    overview = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    overview.add_column(style="dim")
    overview.add_column()
    overview.add_row(text("环境", "Environment"), _display(mode))
    overview.add_row(text("交易对", "Symbol"), symbol)
    overview.add_row(text("持仓", "Position"), _state_text(position, text("空仓", "Flat"), text("持仓中", "Open")))
    overview.add_row(text("当前委托", "Open orders"), _count_or_error(orders))
    overview.add_row(text("API 凭据", "API credentials"), _yes_no(credentials.get("api")))
    if mode == "DEMO":
        overview.add_row(text("网页会话", "Web session"), _yes_no(credentials.get("web")))
    console.print(Panel(overview, title=text("WEEX 状态", "WEEX status"), border_style="cyan"))

    position_rows = position.get("rows")
    if isinstance(position_rows, list) and position_rows:
        console.print(_rows_table(position_rows, title=text("持仓", "Positions")))
    order_rows = orders.get("rows")
    if isinstance(order_rows, list) and order_rows:
        console.print(_rows_table(order_rows, title=text("当前委托", "Open orders")))
    for label, section in ((text("持仓", "Positions"), position), (text("当前委托", "Open orders"), orders)):
        if section.get("error"):
            console.print(f"[yellow]{label}{text('不可用', ' unavailable')}：[/yellow] {section['error']}")


def _render_activity(payload: Mapping[str, Any], console: Console) -> None:
    summary = _mapping(payload.get("summary"))
    asset = str(summary.get("quote_asset") or "USDT")
    complete = payload.get("complete") is True
    status = text("完整", "complete") if complete else text("不完整", "incomplete")
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column(justify="right")
    table.add_row(
        text("时间范围", "Period"),
        f"{payload.get('start_datetime')}  {text('至', 'to')}  {payload.get('end_datetime')}",
    )
    table.add_row(text("总成交量", "Volume"), f"{summary.get('total_quote_volume', '0')} {asset}")
    table.add_row(text("开仓成交量", "Opening"), f"{summary.get('opening_quote_volume', '0')} {asset}")
    table.add_row(text("平仓成交量", "Closing"), f"{summary.get('closing_quote_volume', '0')} {asset}")
    table.add_row("Maker", f"{summary.get('maker_quote_volume', '0')} {asset}")
    table.add_row("Taker", f"{summary.get('taker_quote_volume', '0')} {asset}")
    table.add_row(text("成交记录", "Records"), str(summary.get("trade_count", 0)))
    table.add_row(text("数据覆盖", "Coverage"), status)
    console.print(
        Panel(
            table,
            title=text("交易活动", "Trading activity"),
            border_style="cyan" if complete else "yellow",
        )
    )
    warnings = payload.get("warnings")
    if isinstance(warnings, list):
        for warning in warnings:
            console.print(f"[yellow]{text('提示', 'Note')}：[/yellow] {translate_message(warning)}")
    trades = payload.get("trades")
    if isinstance(trades, list) and trades:
        console.print(_rows_table(trades, title=text("近期成交", "Recent executions")))


def _render_dry_run(payload: Mapping[str, Any], console: Console) -> None:
    plan = _mapping(payload.get("plan"))
    campaign = _mapping(payload.get("campaign"))
    source = plan or campaign or payload
    is_beta = payload.get("kind") == "beta_volume_plan"
    is_beta_campaign = payload.get("kind") == "beta_volume_campaign_plan"
    is_live_volume = payload.get("kind") == "live_maker_volume_plan"
    action = str(
        payload.get("action")
        or (
            "beta_volume"
            if is_beta
            else "beta_campaign"
            if is_beta_campaign
            else "live_maker_volume"
            if is_live_volume
            else "maker_soak"
            if source.get("rounds")
            else "maker_run"
        )
    )
    title = {
        "maker_flatten": text("Maker 平仓", "Maker flatten"),
        "maker_soak": text("Maker 压力测试", "Maker soak"),
        "maker_run": text("Maker 交易量任务", "Maker run"),
        "cancel_order": text("撤销订单", "Cancel order"),
        "beta_volume": text("BTC 多头 / ETH 空头 Beta 交易量", "BTC long / ETH short Beta volume"),
        "beta_campaign": text("Beta Campaign", "Beta campaign"),
        "live_maker_volume": text("实盘交替 Maker 交易量", "Live alternating Maker volume"),
    }.get(action, action.replace("_", " ").title())
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()
    fields = (
        (text("模式", "Mode"), source.get("mode") or payload.get("mode")),
        (text("交易对", "Symbol"), source.get("symbol") or payload.get("symbol")),
        (
            text("目标成交量", "Target volume"),
            _with_unit(source.get("target_quote_volume") or source.get("target_quote"), "SUSDT"),
        ),
        (text("目标成交量", "Target volume"), _with_unit(source.get("target_turnover_quote"), "USDT")),
        (text("目标成交量", "Target volume"), _with_unit(source.get("target_quote"), "USDT")),
        (text("每周期成交量", "Per cycle"), _with_unit(source.get("round_quote"), "USDT")),
        (text("每周期成交量", "Per cycle"), _with_unit(source.get("round_turnover_quote"), "USDT")),
        (
            text("开仓持有时间", "Open hold"),
            _seconds_range_as_minutes(source.get("hold_min_seconds"), source.get("hold_max_seconds")),
        ),
        (
            text("周期间隔", "Cycle gap"),
            _seconds_range_as_minutes(
                source.get("round_gap_min_seconds"),
                source.get("round_gap_max_seconds"),
            ),
        ),
        (text("预计成交量", "Estimated volume"), _with_unit(source.get("estimated_turnover_quote"), "USDT")),
        (text("BTC 多头", "BTC long"), _beta_leg(source, "BTC")),
        (text("ETH 空头", "ETH short"), _beta_leg(source, "ETH")),
        (
            text("杠杆", "Leverage"),
            text("自动（每周期重新计算）", "auto (recalculated each cycle)")
            if source.get("leverage") == "auto"
            else _with_unit(source.get("leverage"), "x"),
        ),
        (text("保证金模式", "Margin"), source.get("margin_mode")),
        (text("Maker 交易腿", "Maker legs"), source.get("fills")),
        (text("轮次数", "Rounds"), source.get("rounds")),
        (text("预计轮次数", "Estimated rounds"), source.get("estimated_rounds")),
        (text("数量", "Quantity"), payload.get("quantity")),
        (
            text("最大持仓", "Max position"),
            _with_unit(source.get("max_position_quote") or payload.get("max_position"), "SUSDT"),
        ),
        (
            text("单腿超时", "Per-leg timeout"),
            _with_unit(source.get("timeout_seconds_per_order") or payload.get("timeout"), "s"),
        ),
        (text("单次尝试超时", "Per-attempt timeout"), _with_unit(source.get("timeout_seconds"), "s")),
        (text("恢复尝试次数", "Recovery attempts"), source.get("recovery_attempts")),
        (text("成交量来源", "Volume source"), source.get("volume_source")),
    )
    for label, value in fields:
        if value not in (None, ""):
            table.add_row(label, _display(value))
    console.print(Panel(table, title=f"{text('演练计划', 'Dry run')} · {title}", border_style="cyan"))
    console.print(
        Panel(str(payload["confirm"]), title=text("精确确认短语", "Exact confirmation"), border_style="yellow")
    )
    if payload.get("execute_command"):
        console.print(
            Panel(str(payload["execute_command"]), title=text("执行命令", "Execute command"), border_style="green")
        )


def _render_beta_volume_result(payload: Mapping[str, Any], console: Console) -> None:
    status = str(payload.get("status") or "unknown")
    style = "green" if status == "completed" else "yellow" if status in {"uncertain", "executing"} else "red"
    accounting = _mapping(payload.get("accounting"))
    overview = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    overview.add_column(style="dim")
    overview.add_column(justify="right")
    overview.add_row(text("结果", "Result"), translate_message(payload.get("reason") or status))
    overview.add_row(
        text("成交量", "Volume"),
        f"{payload.get('executed_quote_volume', '0')} / {payload.get('target_turnover_quote', '0')} USDT",
    )
    overview.add_row(text("完成率", "Achievement"), f"{payload.get('target_achievement_percent', '0')}%")
    overview.add_row(text("超额", "Excess"), f"{payload.get('excess_quote', '0')} USDT")
    overview.add_row(text("耗时", "Elapsed"), _duration(payload.get("elapsed_ms"), milliseconds=True))
    overview.add_row(text("成交来源", "Fill source"), _display(accounting.get("source") or "unknown"))
    overview.add_row(text("成交笔数", "Fills"), str(accounting.get("fill_count", 0)))
    overview.add_row("Maker / Taker", f"{accounting.get('maker_count', 0)} / {accounting.get('taker_count', 0)}")
    overview.add_row(text("Maker 已验证", "Maker verified"), _yes_no(accounting.get("maker_only")))
    overview.add_row(text("手续费", "Fees"), _asset_totals(accounting.get("commission_by_asset")))
    overview.add_row(text("已实现盈亏", "Realized PnL"), f"{accounting.get('realized_pnl', '0')} USDT")
    console.print(Panel(overview, title=f"Beta {text('交易量', 'volume')} · {_display(status)}", border_style=style))

    cycles = payload.get("cycles")
    if isinstance(cycles, list) and cycles:
        cycle_table = Table(box=box.SIMPLE, title=text("配对周期", "Paired cycles"))
        for column, justify in (
            (text("轮次", "Round"), "right"),
            (text("状态", "Status"), "left"),
            (text("成交量", "Volume"), "right"),
            (text("实际 / 目标 Beta", "Beta actual / target"), "right"),
            (text("杠杆", "Leverage"), "right"),
            (text("耗时", "Time"), "right"),
        ):
            cycle_table.add_column(column, justify=justify)
        for row in cycles:
            item = _mapping(row)
            cycle_table.add_row(
                str(item.get("round", "")),
                _display(item.get("status", "")),
                f"{item.get('executed_quote_volume', '0')} USDT",
                f"{_display(item.get('actual_open_beta'))} / {_display(item.get('planned_open_beta'))}",
                _with_unit(item.get("leverage"), "x"),
                _duration(item.get("elapsed_ms"), milliseconds=True),
            )
        console.print(cycle_table)

    legs = payload.get("legs")
    if isinstance(legs, list) and legs:
        table = Table(box=box.SIMPLE, title=text("执行交易腿", "Execution legs"))
        for column, justify in (
            (text("交易腿", "Leg"), "right"),
            (text("操作", "Action"), "left"),
            (text("市场", "Market"), "left"),
            (text("状态", "Status"), "left"),
            (text("成交量", "Volume"), "right"),
            (text("成交笔数", "Fills"), "right"),
            (text("耗时", "Time"), "right"),
            (text("提交 / 撤单", "Submit / Cancel"), "right"),
        ):
            table.add_column(column, justify=justify)
        for row in legs:
            item = _mapping(row)
            table.add_row(
                str(item.get("sequence", "")),
                _display(item.get("action", "")),
                f"{item.get('symbol', '')} {_display(item.get('position_side', ''))}",
                _display(item.get("verification_status") or item.get("status") or ""),
                f"{item.get('quote_volume', '0')} USDT",
                str(item.get("fill_count", 0)),
                _duration(item.get("elapsed_ms"), milliseconds=True),
                f"{_display(item.get('submissions'))} / {_display(item.get('cancels'))}",
            )
        console.print(table)

    positions = _mapping(payload.get("final_positions"))
    console.print(
        f"{text('最终持仓', 'Final positions')}：BTC {text('多头', 'long')} "
        f"[bold]{_display(positions.get('BTC_LONG'))}[/bold]，"
        f"ETH {text('空头', 'short')} [bold]{_display(positions.get('ETH_SHORT'))}[/bold]"
    )
    if payload.get("reconciliation_required"):
        console.print(f"[yellow]{text('需要人工对账', 'Reconciliation required')}：[/yellow] {payload.get('recovery')}")


def _render_beta_volume_recovery_result(payload: Mapping[str, Any], console: Console) -> None:
    status = str(payload.get("status") or "unknown")
    style = "green" if status == "completed" else "yellow" if status == "uncertain" else "red"
    accounting = _mapping(payload.get("accounting"))
    legs = payload.get("legs")
    leg = _mapping(legs[0]) if isinstance(legs, list) and legs else {}
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column(justify="right")
    table.add_row(text("结果", "Result"), translate_message(payload.get("reason") or status))
    table.add_row(
        text("恢复仓位", "Recovered position"),
        f"{payload.get('symbol', '')} {_display(payload.get('position_side'))}",
    )
    table.add_row(text("成交量", "Volume"), f"{payload.get('executed_quote_volume', '0')} USDT")
    table.add_row(text("成交笔数", "Fills"), str(accounting.get("fill_count", 0)))
    table.add_row(
        "Maker / Taker / Unknown",
        f"{accounting.get('maker_count', 0)} / {accounting.get('taker_count', 0)} / "
        f"{accounting.get('unknown_liquidity_count', 0)}",
    )
    table.add_row(text("提交 / 撤单", "Submit / Cancel"), f"{leg.get('submissions', 0)} / {leg.get('cancels', 0)}")
    table.add_row(text("耗时", "Elapsed"), _duration(leg.get("elapsed_ms"), milliseconds=True))
    table.add_row(text("最终持仓", "Final position"), _display(payload.get("final_position")))
    table.add_row(text("手续费", "Fees"), _asset_totals(accounting.get("commission_by_asset")))
    table.add_row(text("已实现盈亏", "Realized PnL"), f"{accounting.get('realized_pnl', '0')} USDT")
    console.print(
        Panel(
            table,
            title=f"Beta Maker {text('恢复', 'recovery')} · {_display(status)}",
            border_style=style,
        )
    )


def _render_beta_campaign_result(payload: Mapping[str, Any], console: Console) -> None:
    status = str(payload.get("status") or "unknown")
    style = "green" if status == "completed" else "yellow" if status == "uncertain" else "red"
    boundary = _mapping(payload.get("final_boundary"))
    overview = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    overview.add_column(style="dim")
    overview.add_column(justify="right")
    overview.add_row(text("结果", "Result"), translate_message(payload.get("reason") or status))
    overview.add_row(
        text("成交量", "Volume"),
        f"{payload.get('executed_quote_volume', '0')} / {payload.get('target_turnover_quote', '0')} USDT",
    )
    overview.add_row(text("剩余", "Remaining"), f"{payload.get('remaining_quote', '0')} USDT")
    overview.add_row(text("超额", "Excess"), f"{payload.get('excess_quote', '0')} USDT")
    overview.add_row(text("子会话", "Child sessions"), f"{payload.get('runs_used', 0)} / {payload.get('max_runs', 0)}")
    overview.add_row(text("Maker 已验证", "Maker verified"), _yes_no(payload.get("maker_only")))
    overview.add_row(text("持仓数", "Positions"), _display(boundary.get("active_position_count", "unknown")))
    overview.add_row(
        text("普通委托 / 条件单", "Regular / trigger orders"),
        f"{_display(boundary.get('regular_order_count'))} / {_display(boundary.get('trigger_order_count'))}",
    )
    overview.add_row(text("耗时", "Elapsed"), _duration(payload.get("elapsed_ms"), milliseconds=True))
    console.print(Panel(overview, title=f"Beta Campaign · {_display(status)}", border_style=style))


def _render_live_maker_volume_result(payload: Mapping[str, Any], console: Console) -> None:
    status = str(payload.get("status") or "unknown")
    style = "green" if status == "completed" else "yellow" if status == "uncertain" else "red"
    overview = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    overview.add_column(style="dim")
    overview.add_column(justify="right")
    overview.add_row(text("结果", "Result"), translate_message(payload.get("reason") or status))
    overview.add_row(
        text("成交量", "Volume"), f"{payload.get('verified_quote', '0')} / {payload.get('target_quote', '0')} USDT"
    )
    overview.add_row(text("剩余", "Remaining"), f"{payload.get('remaining_quote', '0')} USDT")
    overview.add_row(text("完成率", "Achievement"), f"{payload.get('achievement_percent', '0')}%")
    overview.add_row(
        text("轮次", "Rounds"),
        f"{payload.get('rounds_completed', 0)} {text('轮已空仓', 'flat')} / "
        f"{payload.get('rounds_attempted', 0)} {text('轮已尝试', 'attempted')}",
    )
    if payload.get("excess_quote") not in {None, "0", 0}:
        overview.add_row(text("最小精度超额", "Minimum-step excess"), f"{payload.get('excess_quote')} USDT")
    overview.add_row(text("成交笔数", "Fills"), str(payload.get("fill_count", 0)))
    overview.add_row("Maker / Taker", f"{payload.get('maker_count', 0)} / {payload.get('taker_count', 0)}")
    overview.add_row(text("Maker 已验证", "Maker verified"), _yes_no(payload.get("maker_only")))
    overview.add_row(text("手续费", "Fees"), _asset_totals(payload.get("commission_by_asset")))
    overview.add_row(text("已实现盈亏", "Realized PnL"), f"{payload.get('realized_pnl', '0')} USDT")
    overview.add_row(text("耗时", "Elapsed"), _duration(payload.get("elapsed_ms"), milliseconds=True))
    console.print(
        Panel(
            overview, title=f"{text('实盘 Maker 交易量', 'Live Maker volume')} · {_display(status)}", border_style=style
        )
    )

    rounds = payload.get("rounds")
    if isinstance(rounds, list) and rounds:
        table = Table(box=box.SIMPLE, title=text("空仓到空仓轮次", "Flat-to-flat rounds"))
        for column, justify in (
            (text("轮次", "Round"), "right"),
            (text("方向", "Side"), "left"),
            (text("状态", "Status"), "left"),
            (text("成交量", "Volume"), "right"),
            (text("成交笔数", "Fills"), "right"),
            (text("空仓", "Flat"), "left"),
        ):
            table.add_column(column, justify=justify)
        for row in rounds:
            item = _mapping(row)
            table.add_row(
                str(item.get("round", "")),
                _display(item.get("position_side", "")),
                _display(item.get("status", "")),
                f"{item.get('quote_volume', '0')} USDT",
                str(item.get("fill_count", 0)),
                _yes_no(item.get("flat")),
            )
        console.print(table)
    if payload.get("reconciliation_required"):
        console.print(f"[yellow]{text('需要人工对账', 'Reconciliation required')}：[/yellow] {payload.get('recovery')}")


def _render_soak_result(payload: Mapping[str, Any], console: Console) -> None:
    completed = int(payload.get("rounds_completed") or 0)
    requested = int(payload.get("rounds_requested") or 0)
    status = str(payload.get("status") or "unknown")
    style = "green" if status == "completed" else "red"
    headline = Text()
    headline.append(f"{completed}/{requested} {text('轮', 'rounds')}", style="bold")
    headline.append(f"  ·  {payload.get('total_quote_volume', '0')} SUSDT")
    headline.append(f"  ·  {_duration(payload.get('elapsed_seconds'))}")
    console.print(
        Panel(headline, title=f"{text('Maker 压力测试', 'Maker soak')} · {_display(status)}", border_style=style)
    )

    rounds = payload.get("rounds")
    if isinstance(rounds, list):
        table = Table(box=box.SIMPLE, title=text("轮次", "Rounds"))
        for column, justify in (
            (text("轮次", "Round"), "right"),
            (text("状态", "Status"), "left"),
            (text("成交量", "Volume"), "right"),
            (text("耗时", "Time"), "right"),
            (text("提交次数", "Submits"), "right"),
            (text("错误数", "Errors"), "right"),
            (text("持仓", "Position"), "right"),
            (text("委托", "Orders"), "right"),
        ):
            table.add_column(column, justify=justify)
        for row in rounds:
            item = _mapping(row)
            table.add_row(
                str(item.get("round", "")),
                _display(item.get("status", "")),
                f"{item.get('total_quote_volume', '0')} SUSDT",
                _duration(item.get("elapsed_seconds")),
                str(item.get("submission_count", 0)),
                str(item.get("observation_error_count", 0)),
                _display(item.get("final_position")),
                _display(item.get("active_order_count")),
            )
        console.print(table)
    if payload.get("report_path"):
        console.print(f"{text('报告', 'Report')}：[bold]{payload['report_path']}[/bold]")


def _render_maker_result(payload: Mapping[str, Any], console: Console) -> None:
    execution = _mapping(payload.get("execution")) or payload
    status = str(payload.get("status") or execution.get("status") or "unknown")
    style = "green" if status == "completed" else "red"
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column(justify="right")
    table.add_row(
        text("结果", "Result"),
        translate_message(payload.get("reason") or execution.get("reason") or status),
    )
    table.add_row(
        text("成交量", "Volume"),
        f"{execution.get('quote_volume', payload.get('total_quote_volume', '0'))} SUSDT",
    )
    table.add_row(text("耗时", "Elapsed"), _duration_ms_or_seconds(execution, payload))
    table.add_row(
        text("提交次数", "Submissions"),
        str(execution.get("submissions", payload.get("submission_count", 0))),
    )
    table.add_row(text("仅 Maker", "Maker only"), _yes_no(payload.get("maker_only", execution.get("maker_only"))))
    table.add_row(
        text("最终持仓", "Final position"),
        _display(payload.get("final_position", execution.get("final_position"))),
    )
    table.add_row(text("当前委托", "Open orders"), _display(payload.get("active_order_count")))
    console.print(
        Panel(table, title=f"{text('Maker 流程', 'Maker workflow')} · {_display(status)}", border_style=style)
    )
    if payload.get("report_path"):
        console.print(f"{text('报告', 'Report')}：[bold]{payload['report_path']}[/bold]")


def _render_rows(rows: list[Any], console: Console) -> None:
    if not rows:
        console.print(f"[dim]{text('暂无记录。', 'No records.')}[/dim]")
        return
    console.print(_rows_table(rows))


def _rows_table(rows: Sequence[Any], *, title: str | None = None) -> Table:
    mappings = [_mapping(row) for row in rows if isinstance(row, Mapping)]
    if not mappings:
        table = Table(box=box.SIMPLE, title=title)
        table.add_column(text("值", "Value"))
        for row in rows:
            table.add_row(str(row))
        return table
    preferred = (
        "symbol",
        "side",
        "positionSide",
        "position_action",
        "status",
        "size",
        "quantity",
        "executedQty",
        "price",
        "leverage",
        "unrealizePnl",
        "datetime",
        "quote_quantity",
        "maker",
    )
    columns = [key for key in preferred if any(key in row for row in mappings)]
    if not columns:
        columns = [key for key, value in mappings[0].items() if _is_scalar(value)][:8]
    table = Table(box=box.SIMPLE, title=title)
    for column in columns:
        table.add_column(_label(column), justify="right" if column in {"size", "price", "leverage"} else "left")
    for row in mappings:
        table.add_row(*[_display(row.get(column)) for column in columns])
    return table


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _state_text(section: Mapping[str, Any], empty: str, nonempty: str) -> str:
    if section.get("error"):
        return text("不可用", "Unavailable")
    count = int(section.get("count") or 0)
    return empty if count == 0 else f"{nonempty} ({count})"


def _count_or_error(section: Mapping[str, Any]) -> str:
    return text("不可用", "Unavailable") if section.get("error") else str(section.get("count") or 0)


def _yes_no(value: Any) -> str:
    return text("是", "Yes") if value is True else text("否", "No") if value is False else text("未知", "Unknown")


def _display(value: Any) -> str:
    return "—" if value is None or value == "" else translate_value(value)


def _with_unit(value: Any, unit: str) -> str | None:
    return None if value in (None, "") else f"{value} {unit}"


def _range_with_unit(minimum: Any, maximum: Any, unit: str) -> str | None:
    if minimum in (None, "") or maximum in (None, ""):
        return None
    if minimum == maximum:
        return f"{minimum} {unit}"
    return f"{minimum}-{maximum} {unit}"


def _seconds_range_as_minutes(minimum: Any, maximum: Any) -> str | None:
    if minimum in (None, "") or maximum in (None, ""):
        return None
    try:
        minimum_minutes = float(minimum) / 60
        maximum_minutes = float(maximum) / 60
    except (TypeError, ValueError):
        return None
    return _range_with_unit(
        f"{minimum_minutes:g}",
        f"{maximum_minutes:g}",
        text("分钟", "min"),
    )


def _beta_leg(plan: Mapping[str, Any], symbol: str) -> str | None:
    legs = plan.get("legs")
    if not isinstance(legs, list):
        return None
    row = next(
        (item for item in legs if isinstance(item, Mapping) and str(item.get("symbol") or "").upper() == symbol),
        None,
    )
    if row is None:
        return None
    return f"{row.get('quantity')} {symbol} ({_display(row.get('position_side'))})"


def _asset_totals(value: Any) -> str:
    totals = _mapping(value)
    if not totals:
        return "0"
    return ", ".join(f"{amount} {asset}" for asset, amount in sorted(totals.items()))


def _duration(value: Any, *, milliseconds: bool = False) -> str:
    try:
        seconds = float(value) / 1000 if milliseconds else float(value)
    except (TypeError, ValueError):
        return "—"
    minutes, remainder = divmod(seconds, 60)
    return (
        f"{int(minutes)}{text('分', 'm ')}{remainder:.1f}{text('秒', 's')}"
        if minutes >= 1
        else f"{remainder:.1f}{text('秒', 's')}"
    )


def _duration_ms_or_seconds(execution: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    if execution.get("elapsed_ms") is not None:
        return _duration(float(execution["elapsed_ms"]) / 1000)
    return _duration(payload.get("elapsed_seconds"))


def _label(value: str) -> str:
    return translate_field(value)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))

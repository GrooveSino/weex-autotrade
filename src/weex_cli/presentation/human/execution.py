"""Human-readable event timeline for paired Beta execution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rich.console import Console

from weex_cli.presentation.i18n import text, translate_message

from .leg import render_leg_progress
from .shared import display as _display
from .shared import duration as _duration
from .shared import yes_no as _yes_no


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
        render_leg_progress(event, console)
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

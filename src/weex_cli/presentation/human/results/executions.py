"""Result panels for Beta volume execution, recovery, and campaigns."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from weex_cli.presentation.i18n import text, translate_message

from ..shared import asset_totals as _asset_totals
from ..shared import display as _display
from ..shared import duration as _duration
from ..shared import mapping as _mapping
from ..shared import with_unit as _with_unit
from ..shared import yes_no as _yes_no


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

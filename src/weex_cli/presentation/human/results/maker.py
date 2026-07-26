"""Result panels for Demo Maker workflows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from weex_cli.presentation.i18n import text, translate_message

from ..shared import display as _display
from ..shared import duration as _duration
from ..shared import duration_ms_or_seconds as _duration_ms_or_seconds
from ..shared import mapping as _mapping
from ..shared import yes_no as _yes_no


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

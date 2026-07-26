"""Status and historical-activity result panels."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from weex_cli.presentation.i18n import text, translate_message

from ..shared import count_or_error as _count_or_error
from ..shared import display as _display
from ..shared import mapping as _mapping
from ..shared import rows_table as _rows_table
from ..shared import state_text as _state_text
from ..shared import yes_no as _yes_no


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

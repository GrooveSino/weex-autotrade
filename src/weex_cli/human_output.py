from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


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
        console.print(Panel(str(payload.get("message") or ""), border_style=style))
        return True
    return False


def _render_status(payload: Mapping[str, Any], console: Console) -> None:
    mode = str(payload.get("mode") or "unknown").upper()
    symbol = str(payload.get("symbol") or "all").upper()
    position = _mapping(payload.get("position"))
    orders = _mapping(payload.get("orders"))
    credentials = _mapping(payload.get("credentials"))

    overview = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    overview.add_column(style="dim")
    overview.add_column()
    overview.add_row("Environment", mode)
    overview.add_row("Symbol", symbol)
    overview.add_row("Position", _state_text(position, "Flat", "Open"))
    overview.add_row("Open orders", _count_or_error(orders))
    overview.add_row("API credentials", _yes_no(credentials.get("api")))
    if mode == "DEMO":
        overview.add_row("Web session", _yes_no(credentials.get("web")))
    console.print(Panel(overview, title="WEEX status", border_style="cyan"))

    position_rows = position.get("rows")
    if isinstance(position_rows, list) and position_rows:
        console.print(_rows_table(position_rows, title="Positions"))
    order_rows = orders.get("rows")
    if isinstance(order_rows, list) and order_rows:
        console.print(_rows_table(order_rows, title="Open orders"))
    for label, section in (("Positions", position), ("Open orders", orders)):
        if section.get("error"):
            console.print(f"[yellow]{label} unavailable:[/yellow] {section['error']}")


def _render_activity(payload: Mapping[str, Any], console: Console) -> None:
    summary = _mapping(payload.get("summary"))
    asset = str(summary.get("quote_asset") or "USDT")
    status = "complete" if payload.get("complete") is True else "incomplete"
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column(justify="right")
    table.add_row("Period", f"{payload.get('start_datetime')}  to  {payload.get('end_datetime')}")
    table.add_row("Volume", f"{summary.get('total_quote_volume', '0')} {asset}")
    table.add_row("Opening", f"{summary.get('opening_quote_volume', '0')} {asset}")
    table.add_row("Closing", f"{summary.get('closing_quote_volume', '0')} {asset}")
    table.add_row("Maker", f"{summary.get('maker_quote_volume', '0')} {asset}")
    table.add_row("Taker", f"{summary.get('taker_quote_volume', '0')} {asset}")
    table.add_row("Records", str(summary.get("trade_count", 0)))
    table.add_row("Coverage", status)
    console.print(Panel(table, title="Trading activity", border_style="cyan" if status == "complete" else "yellow"))
    warnings = payload.get("warnings")
    if isinstance(warnings, list):
        for warning in warnings:
            console.print(f"[yellow]Note:[/yellow] {warning}")
    trades = payload.get("trades")
    if isinstance(trades, list) and trades:
        console.print(_rows_table(trades, title="Recent executions"))


def _render_dry_run(payload: Mapping[str, Any], console: Console) -> None:
    plan = _mapping(payload.get("plan"))
    source = plan or payload
    action = str(payload.get("action") or ("maker_soak" if source.get("rounds") else "maker_run"))
    title = {
        "maker_flatten": "Maker flatten",
        "maker_soak": "Maker soak",
        "maker_run": "Maker run",
        "cancel_order": "Cancel order",
    }.get(action, action.replace("_", " ").title())
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()
    fields = (
        ("Mode", source.get("mode") or payload.get("mode")),
        ("Symbol", source.get("symbol") or payload.get("symbol")),
        ("Target volume", _with_unit(source.get("target_quote_volume") or source.get("target_quote"), "SUSDT")),
        ("Maker legs", source.get("fills")),
        ("Rounds", source.get("rounds")),
        ("Quantity", payload.get("quantity")),
        ("Max position", _with_unit(source.get("max_position_quote") or payload.get("max_position"), "SUSDT")),
        ("Per-leg timeout", _with_unit(source.get("timeout_seconds_per_order") or payload.get("timeout"), "s")),
    )
    for label, value in fields:
        if value not in (None, ""):
            table.add_row(label, str(value))
    console.print(Panel(table, title=f"Dry run · {title}", border_style="cyan"))
    console.print(Panel(str(payload["confirm"]), title="Exact confirmation", border_style="yellow"))


def _render_soak_result(payload: Mapping[str, Any], console: Console) -> None:
    completed = int(payload.get("rounds_completed") or 0)
    requested = int(payload.get("rounds_requested") or 0)
    status = str(payload.get("status") or "unknown")
    style = "green" if status == "completed" else "red"
    headline = Text()
    headline.append(f"{completed}/{requested} rounds", style="bold")
    headline.append(f"  ·  {payload.get('total_quote_volume', '0')} SUSDT")
    headline.append(f"  ·  {_duration(payload.get('elapsed_seconds'))}")
    console.print(Panel(headline, title=f"Maker soak · {status}", border_style=style))

    rounds = payload.get("rounds")
    if isinstance(rounds, list):
        table = Table(box=box.SIMPLE, title="Rounds")
        for column, justify in (
            ("Round", "right"),
            ("Status", "left"),
            ("Volume", "right"),
            ("Time", "right"),
            ("Submits", "right"),
            ("Errors", "right"),
            ("Position", "right"),
            ("Orders", "right"),
        ):
            table.add_column(column, justify=justify)
        for row in rounds:
            item = _mapping(row)
            table.add_row(
                str(item.get("round", "")),
                str(item.get("status", "")),
                f"{item.get('total_quote_volume', '0')} SUSDT",
                _duration(item.get("elapsed_seconds")),
                str(item.get("submission_count", 0)),
                str(item.get("observation_error_count", 0)),
                _display(item.get("final_position")),
                _display(item.get("active_order_count")),
            )
        console.print(table)
    if payload.get("report_path"):
        console.print(f"Report: [bold]{payload['report_path']}[/bold]")


def _render_maker_result(payload: Mapping[str, Any], console: Console) -> None:
    execution = _mapping(payload.get("execution")) or payload
    status = str(payload.get("status") or execution.get("status") or "unknown")
    style = "green" if status == "completed" else "red"
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column(justify="right")
    table.add_row("Result", str(payload.get("reason") or execution.get("reason") or status))
    table.add_row("Volume", f"{execution.get('quote_volume', payload.get('total_quote_volume', '0'))} SUSDT")
    table.add_row("Elapsed", _duration_ms_or_seconds(execution, payload))
    table.add_row("Submissions", str(execution.get("submissions", payload.get("submission_count", 0))))
    table.add_row("Maker only", _yes_no(payload.get("maker_only", execution.get("maker_only"))))
    table.add_row("Final position", _display(payload.get("final_position", execution.get("final_position"))))
    table.add_row("Open orders", _display(payload.get("active_order_count")))
    console.print(Panel(table, title=f"Maker workflow · {status}", border_style=style))
    if payload.get("report_path"):
        console.print(f"Report: [bold]{payload['report_path']}[/bold]")


def _render_rows(rows: list[Any], console: Console) -> None:
    if not rows:
        console.print("[dim]No records.[/dim]")
        return
    console.print(_rows_table(rows))


def _rows_table(rows: Sequence[Any], *, title: str | None = None) -> Table:
    mappings = [_mapping(row) for row in rows if isinstance(row, Mapping)]
    if not mappings:
        table = Table(box=box.SIMPLE, title=title)
        table.add_column("Value")
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
        return "Unavailable"
    count = int(section.get("count") or 0)
    return empty if count == 0 else f"{nonempty} ({count})"


def _count_or_error(section: Mapping[str, Any]) -> str:
    return "Unavailable" if section.get("error") else str(section.get("count") or 0)


def _yes_no(value: Any) -> str:
    return "Yes" if value is True else "No" if value is False else "Unknown"


def _display(value: Any) -> str:
    return "—" if value is None or value == "" else str(value)


def _with_unit(value: Any, unit: str) -> str | None:
    return None if value in (None, "") else f"{value} {unit}"


def _duration(value: Any) -> str:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "—"
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes)}m {remainder:.1f}s" if minutes >= 1 else f"{remainder:.1f}s"


def _duration_ms_or_seconds(execution: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    if execution.get("elapsed_ms") is not None:
        return _duration(float(execution["elapsed_ms"]) / 1000)
    return _duration(payload.get("elapsed_seconds"))


def _label(value: str) -> str:
    return value.replace("_", " ").replace("positionSide", "position side").title()


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))

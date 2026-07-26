"""Small reusable terminal rendering helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table

from weex_cli.presentation.i18n import text, translate_field, translate_value


def render_rows(rows: list[Any], console: Console) -> None:
    if not rows:
        console.print(f"[dim]{text('暂无记录。', 'No records.')}[/dim]")
        return
    console.print(rows_table(rows))


def rows_table(rows: Sequence[Any], *, title: str | None = None) -> Table:
    mappings = [mapping(row) for row in rows if isinstance(row, Mapping)]
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
        columns = [key for key, value in mappings[0].items() if is_scalar(value)][:8]
    table = Table(box=box.SIMPLE, title=title)
    for column in columns:
        table.add_column(label(column), justify="right" if column in {"size", "price", "leverage"} else "left")
    for row in mappings:
        table.add_row(*[display(row.get(column)) for column in columns])
    return table


def mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def state_text(section: Mapping[str, Any], empty: str, nonempty: str) -> str:
    if section.get("error"):
        return text("不可用", "Unavailable")
    count = int(section.get("count") or 0)
    return empty if count == 0 else f"{nonempty} ({count})"


def count_or_error(section: Mapping[str, Any]) -> str:
    return text("不可用", "Unavailable") if section.get("error") else str(section.get("count") or 0)


def yes_no(value: Any) -> str:
    return text("是", "Yes") if value is True else text("否", "No") if value is False else text("未知", "Unknown")


def display(value: Any) -> str:
    return "—" if value is None or value == "" else translate_value(value)


def with_unit(value: Any, unit: str) -> str | None:
    return None if value in (None, "") else f"{value} {unit}"


def range_with_unit(minimum: Any, maximum: Any, unit: str) -> str | None:
    if minimum in (None, "") or maximum in (None, ""):
        return None
    if minimum == maximum:
        return f"{minimum} {unit}"
    return f"{minimum}-{maximum} {unit}"


def seconds_range_as_minutes(minimum: Any, maximum: Any) -> str | None:
    if minimum in (None, "") or maximum in (None, ""):
        return None
    try:
        minimum_minutes = float(minimum) / 60
        maximum_minutes = float(maximum) / 60
    except (TypeError, ValueError):
        return None
    return range_with_unit(
        f"{minimum_minutes:g}",
        f"{maximum_minutes:g}",
        text("分钟", "min"),
    )


def beta_leg(plan: Mapping[str, Any], symbol: str) -> str | None:
    legs = plan.get("legs")
    if not isinstance(legs, list):
        return None
    row = next(
        (item for item in legs if isinstance(item, Mapping) and str(item.get("symbol") or "").upper() == symbol),
        None,
    )
    if row is None:
        return None
    return f"{row.get('quantity')} {symbol} ({display(row.get('position_side'))})"


def asset_totals(value: Any) -> str:
    totals = mapping(value)
    if not totals:
        return "0"
    return ", ".join(f"{amount} {asset}" for asset, amount in sorted(totals.items()))


def duration(value: Any, *, milliseconds: bool = False) -> str:
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


def duration_ms_or_seconds(execution: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    if execution.get("elapsed_ms") is not None:
        return duration(float(execution["elapsed_ms"]) / 1000)
    return duration(payload.get("elapsed_seconds"))


def label(value: str) -> str:
    return translate_field(value)


def is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))

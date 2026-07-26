"""Plan-preview panels and exact confirmation output."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from weex_cli.presentation.i18n import text

from ..shared import beta_leg as _beta_leg
from ..shared import display as _display
from ..shared import mapping as _mapping
from ..shared import seconds_range_as_minutes as _seconds_range_as_minutes
from ..shared import with_unit as _with_unit


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

"""Human-readable events for alternating live Maker volume sessions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rich.console import Console

from weex_cli.presentation.i18n import text, translate_message

from .shared import display as _display


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

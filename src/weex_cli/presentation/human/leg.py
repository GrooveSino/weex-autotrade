"""Per-order progress details for a paired execution lane."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rich.console import Console

from weex_cli.presentation.i18n import text, translate_message

from .shared import display as _display
from .shared import duration as _duration


def render_leg_progress(event: Mapping[str, Any], console: Console) -> None:
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

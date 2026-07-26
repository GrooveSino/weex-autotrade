"""Live Campaign monitor screen and durable event timeline."""

from __future__ import annotations

import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, RichLog, Static

from ..support import _decimal_or_zero, _event_log_line, _phase_name, _worker_state


class CampaignMonitorScreen(Screen[None]):
    def __init__(self, payload: Mapping[str, Any]) -> None:
        super().__init__()
        self.payload = dict(payload)
        self.worker_state = "正在准备"
        self.event_count = 0
        self.event_names: list[str] = []
        self.last_event = "等待后台 worker 启动"
        self.current_run = 0
        self.completed = Decimal(0)
        self.phase = "启动"
        self.wait_until = 0.0
        self.submissions = 0
        self.cancels = 0
        self.requotes = 0
        self.maker_count = 0
        self.taker_count = 0
        self.unknown_count = 0
        self.btc_quote = Decimal(0)
        self.eth_quote = Decimal(0)

    def compose(self) -> ComposeResult:
        campaign = cast(Mapping[str, Any], self.payload["campaign"])
        yield Header()
        with Vertical(id="page"):
            yield Label(f"Campaign {campaign['campaign_id']}", classes="title")
            yield Static("正在打开实时任务控制台…", id="monitor-status", classes="panel")
            yield Static("", id="monitor-summary", classes="panel")
            yield RichLog(id="events", max_lines=None, wrap=True, highlight=False, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.app.bind_campaign_monitor(self)
        self.set_interval(1.0, self._tick)
        self._render_summary()

    def on_unmount(self) -> None:
        self.app.unbind_campaign_monitor(self)

    def apply_event(self, event: Mapping[str, Any]) -> None:
        name = str(event.get("event") or "unknown")
        self.event_count += 1
        self.event_names.append(name)
        self.last_event = name
        self.worker_state = _worker_state(name, event)
        self.phase = _phase_name(name, event)
        if event.get("run") is not None:
            self.current_run = int(event["run"])
        elif event.get("round") is not None:
            self.current_run = max(self.current_run, int(event["round"]))
        if event.get("total_quote") is not None:
            self.completed = _decimal_or_zero(event["total_quote"])
        if name == "leg_completed":
            quote = _decimal_or_zero(event.get("quote_volume"))
            if str(event.get("symbol")) == "BTC":
                self.btc_quote += quote
            elif str(event.get("symbol")) == "ETH":
                self.eth_quote += quote
            self.submissions += int(event.get("submissions") or 0)
            self.cancels += int(event.get("cancels") or 0)
            self.maker_count += int(event.get("fill_count") or 0)
        if name == "leg_progress":
            progress = str(event.get("progress_event") or "")
            if progress == "requote":
                self.requotes += 1
            elif progress.startswith("cancel"):
                self.cancels += 1
            if event.get("remaining_ms") is not None:
                self.wait_until = time.monotonic() + float(event["remaining_ms"]) / 1000
        if name in {"hold_started", "round_gap_started"}:
            self.wait_until = time.monotonic() + float(event.get("seconds") or 0)
        if name in {"hold_completed", "round_gap_completed"}:
            self.wait_until = 0
        detail = _event_log_line(event)
        self.query_one("#events", RichLog).write(detail, scroll_end=True, animate=False)
        self.query_one("#monitor-status", Static).update(
            f"实时日志已连接：已接收 {self.event_count} 条事件；最近事件：{name}"
        )
        self._render_summary()

    def show_stopping(self) -> None:
        self.worker_state = "已收到安全停止请求"
        self.phase = "等待安全边界后停止"
        self.query_one("#monitor-status", Static).update(
            "已收到安全停止请求；当前阶段会完成可确认的撤单、对账和平仓后退出。"
        )
        self._render_summary()

    def _tick(self) -> None:
        self._render_summary()

    def _render_summary(self) -> None:
        target = _decimal_or_zero(cast(Mapping[str, Any], self.payload["campaign"])["target_turnover_quote"])
        remaining = max(Decimal(0), target - self.completed)
        excess = max(Decimal(0), self.completed - target)
        countdown = max(0, int(self.wait_until - time.monotonic())) if self.wait_until else 0
        self.query_one("#monitor-summary", Static).update(
            "\n".join(
                (
                    f"状态 / 阶段          {self.worker_state} / {self.phase}",
                    f"轮次                 {self.current_run}",
                    f"完成 / 目标           {self.completed} / {target} USDT",
                    f"剩余 / 超额           {remaining} / {excess} USDT",
                    f"BTC / ETH 成交量      {self.btc_quote} / {self.eth_quote} USDT",
                    f"Maker / Taker / Unknown  {self.maker_count} / {self.taker_count} / {self.unknown_count}",
                    f"挂单 / 撤单 / requote  {self.submissions} / {self.cancels} / {self.requotes}",
                    f"当前等待倒计时         {countdown}s",
                    "安全控制             运行页不提供暂停/停止；Ctrl+C 才会进入安全停止确认",
                )
            )
        )

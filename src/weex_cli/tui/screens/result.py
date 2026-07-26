"""Terminal Campaign result and explicit uncertain-reconciliation screens."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Static

from weex_cli.core.errors import SafetyError
from weex_cli.tui.runtime import boundary_is_flat, reconciliation_confirmation

from ..support import _event_log_line, _safe_error


class CampaignResultScreen(Screen[None]):
    def __init__(self, result: Mapping[str, Any]) -> None:
        super().__init__()
        self.result = dict(result)

    def compose(self) -> ComposeResult:
        status = str(self.result.get("status") or "uncertain")
        boundary = cast(Mapping[str, Any], self.result.get("final_boundary") or {})
        metrics = cast(Mapping[str, Any], self.result.get("tui_metrics") or {})
        yield Header()
        with VerticalScroll(id="page"):
            yield Label(f"Campaign {status}", classes=f"title status-{status}")
            yield Static(
                "\n".join(
                    (
                        f"Campaign ID          {self.result.get('campaign_id', '-')}",
                        f"原因                 {self.result.get('reason', '-')}",
                        f"成交量 / 目标         {self.result.get('executed_quote_volume', '0')} / "
                        f"{self.result.get('target_turnover_quote', '0')} USDT",
                        f"剩余 / 超额           {self.result.get('remaining_quote', '0')} / "
                        f"{self.result.get('excess_quote', '0')} USDT",
                        f"耗时                 {float(self.result.get('elapsed_ms') or 0) / 1000:.1f}s",
                        f"最终仓位 / 普通单 / 条件单  {boundary.get('active_position_count', '?')} / "
                        f"{boundary.get('regular_order_count', '?')} / {boundary.get('trigger_order_count', '?')}",
                        f"纯 Maker             {self.result.get('maker_only', False)}",
                        f"BTC / ETH 成交量      {metrics.get('btc_quote', '0')} / {metrics.get('eth_quote', '0')} USDT",
                        f"Maker / Taker / Unknown  {metrics.get('maker_count', 0)} / "
                        f"{metrics.get('taker_count', 0)} / {metrics.get('unknown_count', 0)}",
                        f"挂单 / 撤单 / requote  {metrics.get('submissions', 0)} / "
                        f"{metrics.get('cancels', 0)} / {metrics.get('requotes', 0)}",
                        f"开仓 / 平仓 / 持仓 / 间隔  {metrics.get('open_ms', 0)}ms / "
                        f"{metrics.get('close_ms', 0)}ms / {metrics.get('hold_seconds', 0)}s / "
                        f"{metrics.get('gap_seconds', 0)}s",
                    )
                ),
                classes="panel",
            )
            yield Label("完整任务日志", classes="section-title")
            yield RichLog(id="result-events", max_lines=None, wrap=True, highlight=False, markup=False)
            if status == "uncertain":
                campaign_id = str(self.result.get("campaign_id") or "")
                yield Static("必须在 WEEX 人工核对 BTC/ETH 仓位、普通单、条件单与成交明细。", classes="error")
                yield Static(reconciliation_confirmation(campaign_id), classes="phrase")
                yield Input(placeholder="输入完整人工核对短语", id="reconcile-confirmation")
                yield Button("核对交易所并解除新 Campaign 阻断", id="reconcile", variant="warning")
                yield Static("", id="reconcile-result")
            with Horizontal(classes="actions"):
                yield Button("账户概览", id="overview")
                yield Button("账户列表", id="accounts")
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#result-events", RichLog)
        events = self.result.get("tui_events")
        if not isinstance(events, list):
            log.write("未找到本次任务的持久化日志。", scroll_end=True, animate=False)
            return
        for event in events:
            if isinstance(event, Mapping):
                log.write(_event_log_line(event), scroll_end=True, animate=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "reconcile":
            self.reconcile()
        elif event.button.id == "overview":
            self.app.show_overview()
        elif event.button.id == "accounts":
            self.app.leave_account()

    @work(thread=True, exclusive=True)
    def reconcile(self) -> None:
        confirmation = self.app.call_from_thread(lambda: self.query_one("#reconcile-confirmation", Input).value)
        campaign_id = str(self.result.get("campaign_id") or "")
        try:
            snapshot = self.app.require_workflow().account_boundary()
            if not boundary_is_flat(snapshot):
                raise SafetyError("exchange account is not flat or still has active orders")
            self.app.require_journal().acknowledge_reconciliation(campaign_id, confirmation)
        except Exception as exc:  # noqa: BLE001 - classified error only
            self.app.call_from_thread(
                self.query_one("#reconcile-result", Static).update,
                _safe_error(exc),
            )
            return
        self.app.call_from_thread(
            self.query_one("#reconcile-result", Static).update,
            "人工核对已记录；原 campaign 保持 uncertain，可重新生成新计划。",
        )
        self.app.call_from_thread(setattr, self.query_one("#reconcile", Button), "disabled", True)


class SafeQuitScreen(ModalScreen[None]):
    def compose(self) -> ComposeResult:
        with Vertical(id="quit-dialog"):
            yield Label("Campaign 正在运行")
            yield Static(self.app.stop_confirmation, classes="phrase")
            yield Input(placeholder="输入完整安全停止短语", id="quit-stop-confirmation")
            with Horizontal(classes="actions"):
                yield Button("请求安全停止", id="confirm-stop", variant="warning")
                yield Button("继续监控", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-stop":
            phrase = self.query_one("#quit-stop-confirmation", Input).value
            try:
                self.app.request_safe_stop(phrase)
            except SafetyError as exc:
                self.app.notify(str(exc), severity="error")
                return
            self.dismiss()
        elif event.button.id == "cancel":
            self.dismiss()

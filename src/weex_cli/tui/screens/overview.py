"""Account readiness and unresolved-Campaign boundary screen."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, cast

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Label, Static

from weex_cli.tui.runtime import boundary_is_flat

from ..support import _safe_error
from .campaign import CampaignFormScreen
from .result import CampaignResultScreen


class AccountOverviewScreen(Screen[None]):
    BINDINGS = [("r", "reload", "刷新"), ("escape", "back", "返回")]

    def __init__(self) -> None:
        super().__init__()
        self.unresolved: list[Any] = []

    def compose(self) -> ComposeResult:
        account = self.app.selected_account
        yield Header()
        with Vertical(id="page"):
            yield Label(account.name if account else "账户", classes="title")
            yield Static("正在读取账户状态...", id="overview", classes="panel")
            yield Static("", id="overview-error", classes="error")
            with Horizontal(classes="actions"):
                yield Button("刷新", id="reload")
                yield Button("设置 Campaign", id="campaign", variant="primary", disabled=True)
                yield Button("人工核对 uncertain", id="reconcile-blocker", variant="warning", disabled=True)
                yield Button("返回账户", id="back")
        yield Footer()

    def on_mount(self) -> None:
        try:
            self.unresolved = self.app.require_journal().unresolved_uncertain()
        except Exception as exc:  # noqa: BLE001 - local journal errors fail closed
            self.query_one("#overview-error", Static).update(_safe_error(exc))
        self._refresh_reconcile_button()
        self.load_snapshot()

    @work(thread=True, exclusive=True)
    def load_snapshot(self) -> None:
        try:
            snapshot = self.app.require_workflow().account_snapshot()
        except Exception as exc:  # noqa: BLE001 - UI only exposes a classified, redacted failure
            self.app.call_from_thread(self.show_error, exc)
            return
        self.app.call_from_thread(self.show_snapshot, snapshot)

    def show_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        self.app.last_snapshot = dict(snapshot)
        allocation = cast(Mapping[str, Any], snapshot.get("allocation") or {})
        allocation_available = snapshot.get("allocation_status") == "ok" or bool(allocation)
        age = (
            max(0, int(time.time() * 1000) - int(allocation.get("as_of_ms") or 0)) / 1000
            if allocation_available
            else None
        )
        try:
            self.unresolved = self.app.require_journal().unresolved_uncertain()
        except Exception as exc:  # noqa: BLE001 - local journal errors fail closed
            self.show_error(exc)
            return
        position_sizes = cast(Mapping[str, Any], snapshot.get("position_sizes") or {})
        blocker = f"\n阻断：{len(self.unresolved)} 个 uncertain campaign 尚未人工核对" if self.unresolved else ""
        self.query_one("#overview", Static).update(
            "\n".join(
                (
                    f"API                 {snapshot.get('api_status', 'unknown')}",
                    f"USDT 可用余额       {snapshot.get('available_quote', '0')}",
                    f"BTC / ETH 仓位       {position_sizes.get('BTC', '0')} / {position_sizes.get('ETH', '0')}",
                    f"普通 / 条件挂单      {snapshot.get('regular_order_count', '?')} / "
                    f"{snapshot.get('trigger_order_count', '?')}",
                    f"Final Beta          {allocation.get('beta', '不可用')}",
                    "Beta 版本 / 年龄     "
                    + (
                        f"{allocation.get('version', '?')} / {age:.1f}s"
                        if age is not None
                        else f"不可用 / {snapshot.get('allocation_error', 'beta_unavailable')}"
                    ),
                    "代理                 "
                    f"{self.app.selected_account.proxy_host if self.app.selected_account else '-'}",
                )
            )
            + blocker
        )
        can_start = boundary_is_flat(snapshot) and allocation_available and not self.unresolved
        self.query_one("#campaign", Button).disabled = not can_start
        if can_start:
            blocker_text = ""
        elif not allocation_available:
            blocker_text = "Beta 数据当前不可用，账户数据已正常读取；恢复前不能创建 Campaign"
        else:
            blocker_text = "账户未满足新 Campaign 的安全边界"
        self.query_one("#overview-error", Static).update(blocker_text)
        self._refresh_reconcile_button()

    def show_error(self, exc: Exception) -> None:
        self.query_one("#overview", Static).update("账户状态不可用")
        self.query_one("#overview-error", Static).update(_safe_error(exc))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "reload":
            self.action_reload()
        elif event.button.id == "campaign":
            self.app.push_screen(CampaignFormScreen())
        elif event.button.id == "reconcile-blocker":
            self.show_uncertain()
        elif event.button.id == "back":
            self.action_back()

    def action_reload(self) -> None:
        self.query_one("#campaign", Button).disabled = True
        self.query_one("#overview", Static).update("正在读取账户状态...")
        self.load_snapshot()

    def action_back(self) -> None:
        self.app.leave_account()

    def show_uncertain(self) -> None:
        if not self.unresolved:
            return
        record = self.unresolved[0]
        result = dict(record.result) if isinstance(record.result, Mapping) else {}
        result.update(
            {
                "status": "uncertain",
                "campaign_id": record.campaign.campaign_id,
                "reason": result.get("reason") or "manual_reconciliation_required",
                "target_turnover_quote": result.get("target_turnover_quote")
                or str(record.campaign.target_turnover_quote),
                "retry_allowed": False,
            }
        )
        self.app.push_screen(CampaignResultScreen(result))

    def _refresh_reconcile_button(self) -> None:
        button = self.query_one("#reconcile-blocker", Button)
        button.disabled = not self.unresolved
        button.display = bool(self.unresolved)

"""Account selection screen."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Label, Static

from weex_cli.tui.accounts import AccountLease


class AccountSelectionScreen(Screen[None]):
    BINDINGS = [("r", "refresh", "刷新"), ("q", "quit", "退出")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="page"):
            yield Label("WEEX Beta Campaign", classes="title")
            yield Static("选择账户", classes="section-title")
            with VerticalScroll(id="account-list"):
                for index, account in enumerate(self.app.catalog.accounts):
                    locked = AccountLease.is_locked(account.account_id, self.app.runtime_root)
                    status = "使用中" if locked else ("可用" if account.enabled else "已禁用")
                    label = f"{account.name}  Key ...{account.api_key_tail}  代理 {account.proxy_host}  {status}"
                    yield Button(
                        label,
                        id=f"account-{index}",
                        name=account.account_id,
                        disabled=locked or not account.enabled,
                        classes="account-row",
                    )
            with Horizontal(classes="actions"):
                yield Button("刷新", id="refresh")
                yield Button("退出", id="quit")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh":
            self.action_refresh()
        elif event.button.id == "quit":
            self.app.action_quit()
        elif event.button.name:
            self.app.select_account(event.button.name)

    def action_refresh(self) -> None:
        self.app.refresh_catalog()

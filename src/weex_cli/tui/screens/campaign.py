"""Campaign plan form and its exact-confirmation preview."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, cast

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, Static

from weex_cli.beta_campaign.workflow import CampaignPreviewRequest
from weex_cli.core.errors import SafetyError, ValidationError

from ..support import _nonnegative_decimal, _positive_decimal, _safe_error


class CampaignFormScreen(Screen[None]):
    BINDINGS = [("escape", "back", "返回")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="page"):
            yield Label("Campaign 设置", classes="title")
            with Horizontal(classes="field"):
                yield Label("目标交易量 (USDT)")
                yield Input(value="6000", id="target", type="number")
            with Horizontal(classes="field"):
                yield Label("每轮交易量 (USDT)")
                yield Input(value="500", id="cycle", type="number")
            with Horizontal(classes="field"):
                yield Label("持仓分钟")
                yield Input(value="5", id="hold-min", type="number")
                yield Label("至")
                yield Input(value="7", id="hold-max", type="number")
            with Horizontal(classes="field"):
                yield Label("轮次间隔分钟")
                yield Input(value="5", id="gap-min", type="number")
                yield Label("至")
                yield Input(value="7", id="gap-max", type="number")
            yield Static("方向  BTC 多 / ETH 空    流动性  POST_ONLY    杠杆  AUTO", classes="panel")
            yield Static("", id="form-error", classes="error")
            with Horizontal(classes="actions"):
                yield Button("生成预览", id="preview", variant="primary")
                yield Button("返回", id="back")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "preview":
            self.create_preview()
        elif event.button.id == "back":
            self.action_back()

    def action_back(self) -> None:
        self.app.pop_screen()

    @work(thread=True, exclusive=True)
    def create_preview(self) -> None:
        self.app.call_from_thread(self._set_busy, True)
        try:
            request = self.app.call_from_thread(self._request)
            if self.app.require_journal().unresolved_uncertain():
                raise SafetyError("uncertain campaign must be reconciled before creating a new campaign")
            payload = self.app.require_workflow().preview(request, require_flat=True)
        except Exception as exc:  # noqa: BLE001 - classified error only
            self.app.call_from_thread(self._show_error, exc)
            self.app.call_from_thread(self._set_busy, False)
            return
        self.app.call_from_thread(self.app.show_preview, payload)

    def _request(self) -> CampaignPreviewRequest:
        values = {
            "target": _positive_decimal(self.query_one("#target", Input).value, "target"),
            "cycle": _positive_decimal(self.query_one("#cycle", Input).value, "cycle"),
            **{
                key: _nonnegative_decimal(self.query_one(f"#{key}", Input).value, key)
                for key in ("hold-min", "hold-max", "gap-min", "gap-max")
            },
        }
        if values["cycle"] > values["target"]:
            raise ValidationError("每轮交易量不能大于目标交易量")
        if values["hold-min"] > values["hold-max"] or values["gap-min"] > values["gap-max"]:
            raise ValidationError("时间范围的最小值不能大于最大值")
        if values["hold-max"] > 60 or values["gap-max"] > 60:
            raise ValidationError("持仓和轮次间隔不能超过 60 分钟")
        return CampaignPreviewRequest(
            target_quote=str(values["target"]),
            cycle_volume=str(values["cycle"]),
            hold_min_seconds=float(values["hold-min"] * 60),
            hold_max_seconds=float(values["hold-max"] * 60),
            round_gap_min_seconds=float(values["gap-min"] * 60),
            round_gap_max_seconds=float(values["gap-max"] * 60),
        )

    def _set_busy(self, busy: bool) -> None:
        self.query_one("#preview", Button).disabled = busy

    def _show_error(self, exc: Exception) -> None:
        self.query_one("#form-error", Static).update(_safe_error(exc))


class CampaignPreviewScreen(Screen[None]):
    BINDINGS = [("escape", "back", "返回")]

    def __init__(self, payload: Mapping[str, Any]) -> None:
        super().__init__()
        self.payload = dict(payload)

    def compose(self) -> ComposeResult:
        campaign = cast(Mapping[str, Any], self.payload["campaign"])
        allocation = cast(Mapping[str, Any], campaign["allocation"])
        readiness = cast(Mapping[str, Any], self.payload["account_readiness"])
        beta = Decimal(str(allocation["beta"]))
        btc_ratio = Decimal(1) / (Decimal(1) + beta)
        eth_ratio = beta * btc_ratio
        yield Header()
        with VerticalScroll(id="page"):
            yield Label("Campaign 预览", classes="title")
            yield Static(
                "\n".join(
                    (
                        f"计划 ID             {campaign['campaign_id']}",
                        f"目标 / 每轮          {campaign['target_turnover_quote']} / "
                        f"{campaign['round_turnover_quote']} USDT",
                        f"预计轮数 / 授权上限   {self.payload.get('estimated_cycles')} / "
                        f"{campaign['authorized_max_turnover_quote']} USDT",
                        f"Final Beta          {allocation['beta']}  ({allocation['version']})",
                        f"BTC 多 / ETH 空      {btc_ratio:.4%} / {eth_ratio:.4%}",
                        f"可用余额 / 自动杠杆   {readiness.get('available_quote', '0')} / "
                        f"{readiness.get('planned_leverage', '?')}x",
                        f"最大支持交易量        {self.payload.get('max_supported_turnover_quote', '?')} USDT",
                        f"过期时间             {campaign['expires_at_ms']}",
                    )
                ),
                classes="panel",
            )
            yield Static(str(self.payload["confirm"]), id="exact-phrase", classes="phrase")
            yield Checkbox("我已核对实盘账户、余额、Beta 与纯 Maker 风险", id="risk")
            yield Input(placeholder="输入完整确认短语", id="confirmation")
            yield Static("", id="preview-error", classes="error")
            with Horizontal(classes="actions"):
                yield Button("执行 Campaign", id="execute", variant="error", disabled=True)
                yield Button("返回修改", id="back")
        yield Footer()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        self._refresh_execute()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_execute()

    def _refresh_execute(self) -> None:
        risk = self.query_one("#risk", Checkbox).value
        confirmation = self.query_one("#confirmation", Input).value
        self.query_one("#execute", Button).disabled = not (risk and confirmation == self.payload["confirm"])

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "execute":
            self.app.start_campaign(self.payload)
        elif event.button.id == "back":
            self.action_back()

    def action_back(self) -> None:
        self.app.pop_screen()

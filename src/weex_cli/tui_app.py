from __future__ import annotations

import json
import signal
import threading
import time
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, cast

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, RichLog, Static

from weex_cli.beta_allocation import HttpBetaAllocationProvider
from weex_cli.beta_campaign import live_profile_fingerprint
from weex_cli.beta_campaign_workflow import (
    BetaCampaignApplication,
    CampaignPreviewRequest,
    CampaignRuntimePaths,
)
from weex_cli.errors import SafetyError, ValidationError, WeexCliError
from weex_cli.redaction import redact_text
from weex_cli.tui_accounts import (
    DEFAULT_ACCOUNT_FILE,
    DEFAULT_RUNTIME_DIRECTORY,
    AccountInUseError,
    AccountLease,
    TuiAccount,
    TuiAccountCatalog,
    load_tui_account_catalog,
)
from weex_cli.tui_runtime import TuiCampaignJournal, boundary_is_flat, reconciliation_confirmation


class CampaignWorkflow(Protocol):
    profile_fingerprint: str

    def account_snapshot(self) -> dict[str, Any]: ...

    def account_boundary(self) -> dict[str, Any]: ...

    def preview(self, request: CampaignPreviewRequest, *, require_flat: bool = False) -> dict[str, Any]: ...

    def execute(
        self,
        *,
        confirmation: str,
        campaign_id: str | None = None,
        event_sink: Callable[[Mapping[str, Any]], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]: ...

    def load(self, campaign_id: str) -> Any: ...

    def mark_interrupted_uncertain(self) -> list[str]: ...


WorkflowFactory = Callable[[TuiAccount, TuiAccountCatalog, CampaignRuntimePaths], CampaignWorkflow]
CatalogLoader = Callable[[Path], TuiAccountCatalog]


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


class WeexTuiApp(App[None]):
    TITLE = "WEEX Beta Campaign"
    CSS = """
    Screen { background: #111417; color: #e8ecef; }
    #page { width: 100%; height: 100%; padding: 1 2; }
    .title { text-style: bold; color: #ffffff; height: 3; }
    .section-title { color: #9ba7b0; height: 2; }
    .panel { border: solid #3b464f; padding: 1 2; margin-bottom: 1; }
    .phrase { border-left: thick #d6a84a; padding: 1 2; margin: 1 0; color: #f2d58d; }
    .error { color: #ff7b72; min-height: 1; }
    .actions { height: 3; margin-top: 1; }
    .actions Button { margin-right: 1; }
    .field { height: 3; align-vertical: middle; }
    .field Label { width: 22; }
    .field Input { width: 18; margin-right: 1; }
    .account-row { width: 100%; height: 3; text-align: left; margin-bottom: 1; }
    #events { border: solid #3b464f; height: 1fr; min-height: 8; }
    #quit-dialog { width: 78; height: auto; max-height: 18; padding: 2; border: heavy #d6a84a; background: #171b1f; }
    """
    BINDINGS = [("ctrl+c", "quit", "安全退出")]

    def __init__(
        self,
        catalog: TuiAccountCatalog,
        *,
        runtime_root: Path = DEFAULT_RUNTIME_DIRECTORY,
        workflow_factory: WorkflowFactory | None = None,
        catalog_loader: CatalogLoader | None = None,
    ) -> None:
        super().__init__()
        self.catalog = catalog
        self.runtime_root = runtime_root
        self.workflow_factory = workflow_factory or self._default_workflow
        self.catalog_loader = catalog_loader or (lambda path: load_tui_account_catalog(path))
        self.selected_account: TuiAccount | None = None
        self.workflow: CampaignWorkflow | None = None
        self.journal: TuiCampaignJournal | None = None
        self.lease: AccountLease | None = None
        self.last_snapshot: dict[str, Any] | None = None
        self.active_campaign = False
        self.stop_event = threading.Event()
        self.stop_confirmation = ""
        self.current_campaign_id = ""
        self._campaign_events: list[dict[str, Any]] = []
        self._campaign_monitor: CampaignMonitorScreen | None = None
        self._old_signal_handlers: dict[int, Any] = {}

    def on_mount(self) -> None:
        self.push_screen(AccountSelectionScreen())
        self._install_signal_handlers()

    def on_unmount(self) -> None:
        self._restore_signal_handlers()
        self._release_lease()

    def select_account(self, account_id: str) -> None:
        account = self.catalog.get(account_id)
        if not account.enabled:
            self.notify("账户已禁用", severity="warning")
            return
        lease = AccountLease(account.account_id, self.runtime_root)
        try:
            lease.acquire()
        except AccountInUseError:
            self.notify("该账户正在另一个终端中使用", severity="error")
            return
        try:
            paths = CampaignRuntimePaths.for_account(self.runtime_root, account.account_id)
            workflow = self.workflow_factory(account, self.catalog, paths)
            workflow.mark_interrupted_uncertain()
        except Exception as exc:  # noqa: BLE001 - no private request has run yet
            lease.release()
            self.notify(_safe_error(exc), severity="error")
            return
        self.lease = lease
        self.selected_account = account
        self.workflow = workflow
        self.journal = TuiCampaignJournal(paths)
        self.push_screen(AccountOverviewScreen())

    def refresh_catalog(self) -> None:
        try:
            self.catalog = self.catalog_loader(self.catalog.path)
        except Exception as exc:  # noqa: BLE001
            self.notify(_safe_error(exc), severity="error")
            return
        self.switch_screen(AccountSelectionScreen())

    def show_preview(self, payload: Mapping[str, Any]) -> None:
        self.push_screen(CampaignPreviewScreen(payload))

    def validate_preview_for_execution(self, payload: Mapping[str, Any]) -> None:
        campaign = cast(Mapping[str, Any], payload["campaign"])
        if int(time.time() * 1000) >= int(campaign["expires_at_ms"]):
            raise SafetyError("campaign plan has expired")
        if self.require_journal().unresolved_uncertain():
            raise SafetyError("uncertain campaign must be reconciled before execution")
        account = self.selected_account
        if account is None:
            raise SafetyError("no account is selected")
        current_catalog = self.catalog_loader(self.catalog.path)
        current = current_catalog.get(account.account_id)
        if not current.enabled:
            raise SafetyError("selected account was disabled after preview")
        current_profile = current.live_profile(current_catalog.path, current_catalog.safety)
        current_profile.require_maker_execution()
        if not current_profile.settings.live_trading_enabled:
            raise SafetyError("live trading is disabled; set WEEX_LIVE_TRADING_ENABLED=true before starting the TUI")
        if live_profile_fingerprint(current_profile) != str(campaign["profile_fingerprint"]):
            raise SafetyError("account credentials or proxy changed after preview")
        record = self.require_workflow().load(str(campaign["campaign_id"]))
        if record.state != "planned":
            raise SafetyError("campaign is no longer in planned state")

    def start_campaign(self, payload: Mapping[str, Any]) -> None:
        campaign = cast(Mapping[str, Any], payload["campaign"])
        self.active_campaign = True
        self.stop_event.clear()
        self.stop_confirmation = str(payload["stop_confirm"])
        self.current_campaign_id = str(campaign["campaign_id"])
        self._campaign_events = []
        self.record_campaign_event(
            {
                "event": "tui_campaign_console_opened",
                "campaign_id": self.current_campaign_id,
                "message": "任务已进入队列；等待控制台渲染后启动后台 worker",
            }
        )
        self.push_screen(CampaignMonitorScreen(payload))
        # Starting the worker only after the monitor has rendered prevents the
        # first preflight event from being lost while Textual switches screens.
        self.call_after_refresh(
            self._launch_campaign_worker, str(payload["confirm"]), self.current_campaign_id, payload
        )

    def _launch_campaign_worker(self, confirmation: str, campaign_id: str, payload: Mapping[str, Any]) -> None:
        if not self.active_campaign or campaign_id != self.current_campaign_id:
            return
        self.run_campaign(confirmation, campaign_id, payload)

    @work(thread=True, exclusive=True, group="live-campaign")
    def run_campaign(self, confirmation: str, campaign_id: str, payload: Mapping[str, Any]) -> None:
        def event_sink(event: Mapping[str, Any]) -> None:
            stored = self.require_journal().append_event(campaign_id, event)
            self.call_from_thread(self.apply_campaign_event, stored)

        execution_started = False
        stage = "worker_start"
        try:
            event_sink({"event": "tui_worker_started", "campaign_id": campaign_id})
            stage = "authorization_validation"
            event_sink({"event": "tui_execution_validation_started", "campaign_id": campaign_id})
            self.validate_preview_for_execution(payload)
            event_sink({"event": "tui_execution_validation_completed", "campaign_id": campaign_id})

            stage = "exchange_preflight"
            event_sink({"event": "tui_execution_preflight_started", "campaign_id": campaign_id})
            snapshot = self.require_workflow().account_boundary()
            if not boundary_is_flat(snapshot):
                raise SafetyError("account changed after preview and is no longer flat")
            event_sink({"event": "tui_execution_preflight_completed", "campaign_id": campaign_id})

            stage = "campaign_execution"
            execution_started = True
            result = self.require_workflow().execute(
                confirmation=confirmation,
                campaign_id=campaign_id,
                event_sink=event_sink,
                stop_requested=self.stop_event.is_set,
            )
        except Exception as exc:  # noqa: BLE001 - a started live worker fails closed as uncertain
            reason = f"tui_{stage}_failed:{type(exc).__name__.lower()}"
            status = "uncertain" if execution_started else "stopped"
            result = {
                "schema_version": 1,
                "kind": "beta_volume_campaign_execution",
                "mode": "live",
                "status": status,
                "reason": reason,
                "campaign_id": campaign_id,
                "retry_allowed": False,
            }
            try:
                record = self.require_workflow().load(campaign_id)
                store = getattr(self.require_workflow(), "campaign_store", None)
                if store is not None:
                    store.save(record.campaign, state=status, result=result)
            except Exception:  # noqa: BLE001 - keep original journal available for manual inspection
                pass
            event_sink(
                {
                    "event": "tui_execution_failed",
                    "campaign_id": campaign_id,
                    "stage": stage,
                    "reason": reason,
                    "message": _safe_error(exc),
                }
            )
            event_sink({"event": f"campaign_{status}", "reason": reason, "campaign_id": campaign_id})
        else:
            event_sink(
                {
                    "event": "tui_execution_result_received",
                    "campaign_id": campaign_id,
                    "status": str(result.get("status") or "unknown"),
                    "reason": result.get("reason"),
                }
            )
        self.call_from_thread(self.finish_campaign, result)

    def record_campaign_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        if not self.current_campaign_id:
            raise SafetyError("no active campaign is selected")
        stored = self.require_journal().append_event(self.current_campaign_id, event)
        self.apply_campaign_event(stored)
        return stored

    def apply_campaign_event(self, event: Mapping[str, Any]) -> None:
        self._campaign_events.append(dict(event))
        monitor = self._campaign_monitor
        if monitor is not None and monitor.is_mounted:
            monitor.apply_event(event)

    def bind_campaign_monitor(self, monitor: CampaignMonitorScreen) -> None:
        self._campaign_monitor = monitor
        for event in self._campaign_events:
            monitor.apply_event(event)

    def unbind_campaign_monitor(self, monitor: CampaignMonitorScreen) -> None:
        if self._campaign_monitor is monitor:
            self._campaign_monitor = None

    def finish_campaign(self, result: Mapping[str, Any]) -> None:
        self.record_campaign_event(
            {
                "event": "tui_worker_finished",
                "campaign_id": self.current_campaign_id,
                "status": str(result.get("status") or "unknown"),
                "reason": result.get("reason"),
            }
        )
        self.active_campaign = False
        self.stop_confirmation = ""
        enriched = dict(result)
        events = self.require_journal().events(self.current_campaign_id, limit=10_000)
        enriched["tui_events"] = events
        enriched["tui_metrics"] = _result_metrics(result, events)
        self.switch_screen(CampaignResultScreen(enriched))

    def request_safe_stop(self, confirmation: str) -> None:
        if not self.active_campaign:
            raise SafetyError("no campaign is running")
        if confirmation != self.stop_confirmation:
            raise SafetyError("safe stop confirmation does not match exactly")
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        workflow = self.require_workflow()
        try:
            record = workflow.load(self.current_campaign_id)
            store = getattr(workflow, "campaign_store", None)
            if store is not None and record.state == "executing":
                store.save(record.campaign, state="stopping", result=record.result)
        except Exception:  # noqa: BLE001 - stop flag remains authoritative in this process
            pass
        self.record_campaign_event(
            {"event": "stop_requested", "campaign_id": self.current_campaign_id},
        )
        if self._campaign_monitor is not None and self._campaign_monitor.is_mounted:
            self._campaign_monitor.show_stopping()

    def show_overview(self) -> None:
        self.switch_screen(AccountOverviewScreen())

    def leave_account(self) -> None:
        if self.active_campaign:
            self.push_screen(SafeQuitScreen())
            return
        self._release_lease()
        self.selected_account = None
        self.workflow = None
        self.journal = None
        self.switch_screen(AccountSelectionScreen())

    def action_quit(self) -> None:
        if self.active_campaign:
            if not isinstance(self.screen, SafeQuitScreen):
                self.push_screen(SafeQuitScreen())
            return
        self._release_lease()
        self.exit()

    def require_workflow(self) -> CampaignWorkflow:
        if self.workflow is None:
            raise SafetyError("no account workflow is active")
        return self.workflow

    def require_journal(self) -> TuiCampaignJournal:
        if self.journal is None:
            raise SafetyError("no account journal is active")
        return self.journal

    def _default_workflow(
        self,
        account: TuiAccount,
        catalog: TuiAccountCatalog,
        paths: CampaignRuntimePaths,
    ) -> CampaignWorkflow:
        profile = account.live_profile(catalog.path, catalog.safety)
        return BetaCampaignApplication(
            profile,
            paths,
            provider_factory=lambda: HttpBetaAllocationProvider(catalog.beta_url),
        )

    def _release_lease(self) -> None:
        if self.lease is not None:
            self.lease.release()
            self.lease = None

    def _install_signal_handlers(self) -> None:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            try:
                self._old_signal_handlers[signal_number] = signal.getsignal(signal_number)
                signal.signal(signal_number, self._handle_signal)
            except (ValueError, OSError):
                continue

    def _restore_signal_handlers(self) -> None:
        for signal_number, handler in self._old_signal_handlers.items():
            try:
                signal.signal(signal_number, handler)
            except (ValueError, OSError):
                continue
        self._old_signal_handlers.clear()

    def _handle_signal(self, signal_number: int, frame: Any) -> None:
        if self.active_campaign:
            self.call_later(self.push_screen, SafeQuitScreen())
        else:
            self.call_later(self.action_quit)


def run_tui(
    accounts_file: Path = DEFAULT_ACCOUNT_FILE,
    *,
    runtime_root: Path = DEFAULT_RUNTIME_DIRECTORY,
) -> None:
    catalog = load_tui_account_catalog(accounts_file)
    WeexTuiApp(catalog, runtime_root=runtime_root).run()


def _positive_decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{name} 必须是数字") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValidationError(f"{name} 必须大于 0")
    return parsed


def _nonnegative_decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{name} 必须是数字") from None
    if not parsed.is_finite() or parsed < 0:
        raise ValidationError(f"{name} 不能小于 0")
    return parsed


def _decimal_or_zero(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return parsed if parsed.is_finite() else Decimal(0)


def _phase_name(event: str, payload: Mapping[str, Any]) -> str:
    if event == "tui_campaign_console_opened":
        return "任务控制台已打开"
    if event == "tui_worker_started":
        return "后台 worker 已启动"
    if event == "tui_execution_validation_started":
        return "执行授权校验"
    if event == "tui_execution_validation_completed":
        return "执行授权校验完成"
    if event == "tui_execution_preflight_started":
        return "实盘账户预检"
    if event == "tui_execution_preflight_completed":
        return "实盘账户预检完成"
    if event == "tui_execution_failed":
        return f"{payload.get('stage', 'worker')} 失败"
    if event == "tui_execution_result_received":
        return "收到 Campaign 结果"
    if event == "tui_worker_finished":
        return "后台 worker 已结束"
    if event.startswith("campaign_child_planning") or event == "cycle_preparing":
        return "轮次规划"
    if event in {"hold_started", "hold_completed"}:
        return "持仓等待"
    if event in {"round_gap_started", "round_gap_completed"}:
        return "轮次间隔"
    if event.startswith("campaign_boundary") or event.startswith("final_acceptance"):
        return "账户边界核对"
    if event.startswith("leg"):
        action = "开仓" if payload.get("action") == "open" else "平仓"
        return f"{payload.get('symbol', '')} {action}".strip()
    if event == "campaign_finished":
        return "完成"
    return event.replace("_", " ")


def _worker_state(event: str, payload: Mapping[str, Any]) -> str:
    if event == "tui_campaign_console_opened":
        return "已排队"
    if event in {"tui_worker_started", "tui_execution_validation_started", "tui_execution_preflight_started"}:
        return "正在启动"
    if event == "tui_execution_failed":
        return "启动失败" if payload.get("stage") != "campaign_execution" else "执行异常"
    if event in {"campaign_stopped", "stop_requested"}:
        return "安全停止中"
    if event in {"campaign_finished", "tui_worker_finished", "tui_execution_result_received"}:
        status = str(payload.get("status") or "")
        if status:
            return status
    return "运行中"


def _event_log_line(event: Mapping[str, Any]) -> str:
    try:
        timestamp_ms = int(event.get("timestamp_ms") or int(time.time() * 1000))
    except (TypeError, ValueError):
        timestamp_ms = int(time.time() * 1000)
    timestamp = time.strftime("%H:%M:%S", time.localtime(timestamp_ms / 1000))
    payload = {key: value for key, value in event.items() if key != "timestamp_ms"}
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return f"[{timestamp}] {rendered}"


def _result_metrics(result: Mapping[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "btc_quote": Decimal(0),
        "eth_quote": Decimal(0),
        "maker_count": 0,
        "taker_count": 0,
        "unknown_count": 0,
        "submissions": 0,
        "cancels": 0,
        "requotes": 0,
        "open_ms": 0,
        "close_ms": 0,
        "hold_seconds": 0.0,
        "gap_seconds": 0.0,
    }
    children = result.get("children")
    if isinstance(children, list):
        for child in children:
            if not isinstance(child, Mapping):
                continue
            cycles = child.get("cycles")
            if not isinstance(cycles, list):
                continue
            for cycle in cycles:
                if not isinstance(cycle, Mapping):
                    continue
                metrics["hold_seconds"] += float(cycle.get("hold_seconds") or 0)
                metrics["gap_seconds"] += float(cycle.get("round_gap_seconds") or 0)
                legs = cycle.get("legs")
                if not isinstance(legs, list):
                    continue
                for leg in legs:
                    if not isinstance(leg, Mapping):
                        continue
                    symbol = str(leg.get("symbol") or "").lower()
                    if symbol == "btc":
                        metrics["btc_quote"] += _decimal_or_zero(leg.get("quote_volume"))
                    elif symbol == "eth":
                        metrics["eth_quote"] += _decimal_or_zero(leg.get("quote_volume"))
                    metrics["maker_count"] += int(leg.get("maker_count") or 0)
                    metrics["taker_count"] += int(leg.get("taker_count") or 0)
                    metrics["unknown_count"] += int(leg.get("unknown_liquidity_count") or 0)
                    metrics["submissions"] += int(leg.get("submissions") or 0)
                    metrics["cancels"] += int(leg.get("cancels") or 0)
                    duration_key = "open_ms" if leg.get("action") == "open" else "close_ms"
                    metrics[duration_key] += int(leg.get("elapsed_ms") or 0)
    metrics["requotes"] = sum(
        1 for event in events if event.get("event") == "leg_progress" and event.get("progress_event") == "requote"
    )
    metrics["btc_quote"] = str(metrics["btc_quote"])
    metrics["eth_quote"] = str(metrics["eth_quote"])
    return metrics


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, SafetyError | ValidationError | WeexCliError):
        return redact_text(exc)
    return f"操作失败 ({type(exc).__name__})；未显示底层响应以保护账户凭据"

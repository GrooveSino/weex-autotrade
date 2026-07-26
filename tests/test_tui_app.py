from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Static

from weex_cli.beta_campaign import live_profile_fingerprint
from weex_cli.beta_campaign.workflow import CampaignPreviewRequest, CampaignRuntimePaths
from weex_cli.tui.accounts import AccountLease, TuiAccount, TuiAccountCatalog, TuiSafety
from weex_cli.tui.app import (
    AccountOverviewScreen,
    AccountSelectionScreen,
    CampaignFormScreen,
    CampaignMonitorScreen,
    CampaignPreviewScreen,
    CampaignResultScreen,
    WeexTuiApp,
)


def catalog(tmp_path: Path, *, count: int = 1) -> TuiAccountCatalog:
    path = tmp_path / "accounts.toml"
    path.write_text("test", encoding="utf-8")
    path.chmod(0o600)
    accounts = tuple(
        TuiAccount(
            account_id=f"account-{index + 1:02d}",
            name=f"Account {index + 1:02d}",
            enabled=True,
            api_key=f"api-key-{index + 1:04d}",
            api_secret=f"secret-{index + 1}",
            passphrase=f"pass-{index + 1}",
            proxy_scheme="http",
            proxy=f"127.0.0.{index + 1}:8080:user:password",
        )
        for index in range(count)
    )
    return TuiAccountCatalog(
        path=path,
        safety=TuiSafety(True, True),
        beta_url="https://beta.private.test/api/v1/hedge-ratio",
        accounts=accounts,
    )


class FakeWorkflow:
    def __init__(self, account: TuiAccount, account_catalog: TuiAccountCatalog) -> None:
        self.account = account
        self.profile_fingerprint = live_profile_fingerprint(
            account.live_profile(account_catalog.path, account_catalog.safety)
        )
        self.preview_payload: dict | None = None
        self.execute_started = threading.Event()
        self.allow_finish = threading.Event()
        self.executions = 0
        self.terminal_status = "stopped"

    def mark_interrupted_uncertain(self) -> list[str]:
        return []

    def account_snapshot(self) -> dict:
        now = int(time.time() * 1000)
        return {
            "api_status": "ok",
            "available_quote": "1000",
            "active_position_count": 0,
            "regular_order_count": 0,
            "trigger_order_count": 0,
            "allocation": {
                "beta": "0.5",
                "version": "beta-test",
                "as_of_ms": now,
                "source": "test",
            },
        }

    def account_boundary(self) -> dict:
        snapshot = self.account_snapshot()
        snapshot.pop("allocation", None)
        return snapshot

    def preview(self, request: CampaignPreviewRequest, *, require_flat: bool = False) -> dict:
        campaign_id = "wc-1234567890"
        confirm = f"EXECUTE WEEX LIVE BETA-CAMPAIGN {campaign_id.upper()} RUNS_20 POST_ONLY"
        self.preview_payload = {
            "campaign": {
                "campaign_id": campaign_id,
                "profile_fingerprint": self.profile_fingerprint,
                "target_turnover_quote": request.target_quote,
                "round_turnover_quote": request.cycle_volume,
                "authorized_max_turnover_quote": "6500",
                "expires_at_ms": int(time.time() * 1000) + 60_000,
                "allocation": {"beta": "0.5", "version": "beta-test"},
            },
            "account_readiness": {"available_quote": "1000", "planned_leverage": 1},
            "estimated_cycles": 12,
            "max_supported_turnover_quote": "100000",
            "confirm": confirm,
            "stop_confirm": f"STOP WEEX LIVE BETA-CAMPAIGN {campaign_id.upper()} POST_ONLY",
        }
        return self.preview_payload

    def load(self, campaign_id: str) -> SimpleNamespace:
        return SimpleNamespace(state="planned")

    def execute(self, *, confirmation, campaign_id=None, event_sink=None, stop_requested=None) -> dict:
        self.executions += 1
        self.execute_started.set()
        if event_sink:
            event_sink({"event": "campaign_run_started", "run": 1, "remaining_quote": "6000"})
            event_sink(
                {
                    "event": "leg_completed",
                    "round": 1,
                    "symbol": "BTC",
                    "action": "open",
                    "quote_volume": "125",
                    "submissions": 1,
                    "cancels": 0,
                }
            )
        while not self.allow_finish.wait(0.01):
            if stop_requested and stop_requested():
                break
        status = self.terminal_status
        return {
            "status": status,
            "reason": "stop_requested" if status == "stopped" else "submission_uncertain",
            "campaign_id": campaign_id,
            "target_turnover_quote": "6000",
            "executed_quote_volume": "250",
            "remaining_quote": "5750",
            "excess_quote": "0",
            "elapsed_ms": 1000,
            "maker_only": status != "uncertain",
            "final_boundary": {
                "active_position_count": 0,
                "regular_order_count": 0,
                "trigger_order_count": 0,
            },
        }


def test_default_workflow_uses_catalog_beta_endpoint(tmp_path: Path) -> None:
    account_catalog = catalog(tmp_path)
    app = WeexTuiApp(account_catalog, runtime_root=tmp_path / "runtime")

    workflow = app._default_workflow(
        account_catalog.accounts[0],
        account_catalog,
        CampaignRuntimePaths.for_account(tmp_path / "runtime", "account-01"),
    )

    provider = workflow.provider_factory()  # type: ignore[attr-defined]
    assert provider.url == account_catalog.beta_url


@pytest.mark.asyncio
async def test_overview_keeps_account_visible_and_blocks_campaign_when_beta_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEEX_LIVE_TRADING_ENABLED", "true")
    account_catalog = catalog(tmp_path)
    workflow = FakeWorkflow(account_catalog.accounts[0], account_catalog)
    workflow.account_snapshot = lambda: {
        "api_status": "ok",
        "available_quote": "1000",
        "active_position_count": 0,
        "position_sizes": {"BTC": "0", "ETH": "0"},
        "regular_order_count": 0,
        "trigger_order_count": 0,
        "allocation_status": "unavailable",
        "allocation_error": "beta_request_failed:timeout",
    }
    app = WeexTuiApp(
        account_catalog,
        runtime_root=tmp_path / "runtime",
        workflow_factory=lambda account, selected_catalog, paths: workflow,
        catalog_loader=lambda path: account_catalog,
    )

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        await pilot.click("#account-0")
        await pilot.pause()
        overview = app.screen.query_one("#overview")
        assert "USDT 可用余额       1000" in str(overview.render())
        assert "Final Beta          不可用" in str(overview.render())
        assert app.screen.query_one("#campaign").disabled is True
        assert "Beta 数据当前不可用" in str(app.screen.query_one("#overview-error").render())


@pytest.mark.asyncio
async def test_full_tui_flow_renders_worker_logs_before_campaign_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEEX_LIVE_TRADING_ENABLED", "true")
    account_catalog = catalog(tmp_path)
    workflows: list[FakeWorkflow] = []

    def factory(account, selected_catalog, paths):
        assert AccountLease.is_locked(account.account_id, tmp_path / "runtime")
        workflow = FakeWorkflow(account, selected_catalog)
        workflows.append(workflow)
        return workflow

    app = WeexTuiApp(
        account_catalog,
        runtime_root=tmp_path / "runtime",
        workflow_factory=factory,
        catalog_loader=lambda path: account_catalog,
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, AccountSelectionScreen)
        await pilot.click("#account-0")
        await pilot.pause()
        assert isinstance(app.screen, AccountOverviewScreen)
        assert workflows and workflows[0].account_snapshot()
        await pilot.click("#campaign")
        assert isinstance(app.screen, CampaignFormScreen)
        await pilot.click("#preview")
        await pilot.pause()
        assert isinstance(app.screen, CampaignPreviewScreen)
        execute_button = app.screen.query_one("#execute")
        assert execute_button.disabled is True
        app.screen.query_one("#risk").value = True
        app.screen.query_one("#confirmation").value = workflows[0].preview_payload["confirm"]
        await pilot.pause()
        assert execute_button.disabled is False
        await pilot.click("#execute")
        await pilot.pause()
        assert isinstance(app.screen, CampaignMonitorScreen)
        assert workflows[0].execute_started.wait(1)
        assert app.active_campaign is True
        monitor = app.screen
        try:
            for _ in range(20):
                if monitor.event_count >= 6:
                    break
                await pilot.pause(0.05)
            assert monitor.event_count >= 6
            assert "tui_campaign_console_opened" in monitor.event_names
            assert "tui_worker_started" in monitor.event_names
            assert "tui_execution_preflight_completed" in monitor.event_names
            assert "实时日志已连接" in str(monitor.query_one("#monitor-status", Static).render())
            assert len(monitor.query("#stop")) == 0
            assert len(monitor.query("#stop-confirmation")) == 0
        finally:
            workflows[0].allow_finish.set()

        await pilot.pause()
        for _ in range(20):
            if isinstance(app.screen, CampaignResultScreen):
                break
            await pilot.pause(0.05)
        assert isinstance(app.screen, CampaignResultScreen)
        assert workflows[0].executions == 1
        result_events = app.screen.result["tui_events"]
        assert any(event.get("event") == "tui_worker_started" for event in result_events)
        assert len(app.screen.query("#result-events")) == 1

    assert AccountLease.is_locked("account-01", tmp_path / "runtime") is False


@pytest.mark.asyncio
async def test_same_account_lock_prevents_workflow_creation_before_private_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEEX_LIVE_TRADING_ENABLED", "true")
    account_catalog = catalog(tmp_path)
    calls = 0

    def factory(account, selected_catalog, paths):
        nonlocal calls
        calls += 1
        return FakeWorkflow(account, selected_catalog)

    lease = AccountLease("account-01", tmp_path / "runtime").acquire()
    try:
        app = WeexTuiApp(
            account_catalog,
            runtime_root=tmp_path / "runtime",
            workflow_factory=factory,
            catalog_loader=lambda path: account_catalog,
        )
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            button = app.screen.query_one("#account-0")
            assert button.disabled is True
            assert calls == 0
    finally:
        lease.release()


@pytest.mark.asyncio
async def test_overview_exposes_manual_reconciliation_for_restart_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEEX_LIVE_TRADING_ENABLED", "true")
    account_catalog = catalog(tmp_path)
    workflow = FakeWorkflow(account_catalog.accounts[0], account_catalog)
    app = WeexTuiApp(
        account_catalog,
        runtime_root=tmp_path / "runtime",
        workflow_factory=lambda account, selected_catalog, paths: workflow,
        catalog_loader=lambda path: account_catalog,
    )
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        await pilot.click("#account-0")
        await pilot.pause()
        assert isinstance(app.screen, AccountOverviewScreen)
        app.screen.unresolved = [
            SimpleNamespace(
                campaign=SimpleNamespace(campaign_id="wc-1234567890", target_turnover_quote="6000"),
                result={"reason": "tui_process_restart"},
            )
        ]
        app.screen._refresh_reconcile_button()
        button = app.screen.query_one("#reconcile-blocker")
        assert button.display is True and button.disabled is False
        app.screen.show_uncertain()
        await pilot.pause()
        assert isinstance(app.screen, CampaignResultScreen)
        assert len(app.screen.query("#reconcile")) == 1
        assert len(app.screen.query("#retry")) == 0


@pytest.mark.asyncio
async def test_uncertain_result_has_manual_reconciliation_but_no_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEEX_LIVE_TRADING_ENABLED", "true")
    account_catalog = catalog(tmp_path)
    workflow = FakeWorkflow(account_catalog.accounts[0], account_catalog)
    workflow.terminal_status = "uncertain"
    workflow.allow_finish.set()
    app = WeexTuiApp(
        account_catalog,
        runtime_root=tmp_path / "runtime",
        workflow_factory=lambda account, selected_catalog, paths: workflow,
        catalog_loader=lambda path: account_catalog,
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#account-0")
        await pilot.pause()
        await pilot.click("#campaign")
        await pilot.click("#preview")
        await pilot.pause()
        app.screen.query_one("#risk").value = True
        app.screen.query_one("#confirmation").value = workflow.preview_payload["confirm"]
        await pilot.pause()
        await pilot.click("#execute")
        for _ in range(20):
            if isinstance(app.screen, CampaignResultScreen):
                break
            await pilot.pause(0.05)
        assert isinstance(app.screen, CampaignResultScreen)
        assert len(app.screen.query("#reconcile")) == 1
        assert len(app.screen.query("#retry")) == 0
        assert len(app.screen.query("#continue")) == 0

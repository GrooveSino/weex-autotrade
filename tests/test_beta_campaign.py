from __future__ import annotations

import json
from dataclasses import replace
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import ccxt
import pytest
from rich.console import Console
from typer._click.utils import strip_ansi
from typer.testing import CliRunner

from weex_cli.beta_allocation import BetaAllocation
from weex_cli.beta_campaign import (
    BetaVolumeCampaign,
    BetaVolumeCampaignStore,
    LiveBetaVolumeCampaignService,
    campaign_confirmation,
    campaign_execute_command,
    campaign_id_from_confirmation,
)
from weex_cli.beta_volume import BetaVolumePlanStore
from weex_cli.cli import app
from weex_cli.commands import live
from weex_cli.config import Settings
from weex_cli.errors import SafetyError, ValidationError
from weex_cli.human_output import render_execution_event, render_human
from weex_cli.live_profile import LiveProfile

runner = CliRunner()


class Gateway:
    def order_book(self, symbol: str, limit: int = 5) -> dict[str, object]:
        mid = Decimal("100") if symbol == "BTC" else Decimal("50")
        return {"bids": [[float(mid - 1), 10]], "asks": [[float(mid + 1), 10]]}

    def amount_step(self, symbol: str) -> Decimal:
        return Decimal("0.1") if symbol == "BTC" else Decimal("0.2")

    def amount_to_precision(self, symbol: str, amount: Decimal) -> Decimal:
        step = self.amount_step(symbol)
        return (amount / step).to_integral_value(rounding=ROUND_DOWN) * step

    def account_balance_rows(self, mode: str) -> list[dict[str, str]]:
        return [{"asset": "USDT", "availableBalance": "1000"}]

    def positions(self, mode: str, symbol: str | None = None) -> list[dict[str, object]]:
        return []

    def open_orders(
        self,
        symbol: str | None = None,
        *,
        mode: str = "live",
        trigger: bool = False,
    ) -> list[dict[str, object]]:
        return []

    def algo_orders(self, symbol: str | None = None) -> list[dict[str, object]]:
        return []


class BoundaryNetworkGateway(Gateway):
    def __init__(self) -> None:
        self.balance_reads = 0

    def account_balance_rows(self, mode: str) -> list[dict[str, str]]:
        self.balance_reads += 1
        if self.balance_reads > 1:
            raise ccxt.NetworkError("boundary unavailable")
        return super().account_balance_rows(mode)


class FlakyPlanningGateway(Gateway):
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.order_book_calls = 0

    def order_book(self, symbol: str, limit: int = 5) -> dict[str, object]:
        self.order_book_calls += 1
        if self.order_book_calls <= self.failures:
            raise ccxt.RequestTimeout("temporary planning timeout")
        return super().order_book(symbol, limit)


class Provider:
    def __init__(self, allocation: BetaAllocation) -> None:
        self.allocation = allocation

    def get(self) -> BetaAllocation:
        return self.allocation


@pytest.fixture
def allocation() -> BetaAllocation:
    return BetaAllocation(
        beta=Decimal("1"),
        btc_long_weight=Decimal("0.5"),
        eth_short_weight=Decimal("0.5"),
        version="beta-v1:1000",
        as_of_ms=1000,
        confidence=Decimal("0.8"),
        confidence_threshold=Decimal("0.65"),
        source="test",
    )


def make_campaign(
    allocation: BetaAllocation,
    *,
    target: str = "200",
    round_quote: str = "200",
    max_runs: int = 20,
    hold_min: float = 0,
    hold_max: float = 0,
    round_gap_min: float = 1,
    round_gap_max: float = 1,
) -> BetaVolumeCampaign:
    return BetaVolumeCampaign.create(
        Gateway(),  # type: ignore[arg-type]
        allocation,
        profile_fingerprint="profile-1234567890",
        target_turnover_quote=target,
        round_turnover_quote=round_quote,
        max_runs=max_runs,
        hold_min_seconds=hold_min,
        hold_max_seconds=hold_max,
        round_gap_min_seconds=round_gap_min,
        round_gap_max_seconds=round_gap_max,
        now_ms=1000,
    )


def test_beta_campaign_default_leg_timeout_is_60_seconds(allocation: BetaAllocation) -> None:
    campaign = make_campaign(allocation)

    assert campaign.timeout_seconds == 60


def authoritative_result(quote: str, *, status: str = "completed", reason: str = "paired_target_completed") -> dict:
    fill_count = 4 if Decimal(quote) > 0 else 0
    return {
        "status": status,
        "reason": reason,
        "executed_quote_volume": quote,
        "accounting": {
            "verified": fill_count > 0,
            "maker_only": fill_count > 0,
            "fill_count": fill_count,
            "maker_count": fill_count,
            "taker_count": 0,
            "unknown_liquidity_count": 0,
        },
    }


def service(
    tmp_path: Path,
    allocation: BetaAllocation,
    executor,
    *,
    fingerprint: str = "profile-1234567890",
    delays: list[float] | None = None,
    gateway: Gateway | None = None,
) -> LiveBetaVolumeCampaignService:
    return LiveBetaVolumeCampaignService(
        gateway or Gateway(),  # type: ignore[arg-type]
        Provider(allocation),  # type: ignore[arg-type]
        BetaVolumeCampaignStore(tmp_path / "campaigns"),
        BetaVolumePlanStore(tmp_path / "children"),
        profile_fingerprint=fingerprint,
        child_executor=executor,
        now_ms=lambda: 1000,
        sleep=(delays if delays is not None else []).append,
    )


def test_campaign_store_rejects_tampering_and_reexecution(tmp_path: Path, allocation: BetaAllocation) -> None:
    campaign = make_campaign(allocation)
    store = BetaVolumeCampaignStore(tmp_path)
    path = store.create(campaign)

    assert store.load(campaign.campaign_id).campaign == campaign
    assert campaign_confirmation(campaign) == (
        f"EXECUTE WEEX LIVE BETA-CAMPAIGN {campaign.campaign_id.upper()} RUNS_20 POST_ONLY"
    )

    store.claim_for_execution(campaign)
    with pytest.raises(SafetyError, match="pristine"):
        store.claim_for_execution(campaign)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["campaign"]["target_turnover_quote"] = "999"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError, match="identity"):
        store.load(campaign.campaign_id)


def test_campaign_normalizes_round_and_binds_the_authorized_ceiling(allocation: BetaAllocation) -> None:
    campaign = make_campaign(allocation, target="200", round_quote="500")

    assert campaign.round_turnover_quote == Decimal("200")
    assert campaign.authorized_max_turnover_quote == Decimal("400")


@pytest.mark.parametrize(
    ("timing", "message"),
    [
        ({"hold_min": 3, "hold_max": 2}, "hold range"),
        ({"hold_min": -1}, "hold range"),
        ({"hold_max": 3601}, "hold range"),
        ({"hold_min": float("nan")}, "hold range"),
        ({"round_gap_min": 3, "round_gap_max": 2}, "round_gap range"),
        ({"round_gap_min": -1}, "round_gap range"),
        ({"round_gap_max": 3601}, "round_gap range"),
        ({"round_gap_max": float("inf")}, "round_gap range"),
    ],
)
def test_campaign_rejects_invalid_timing_ranges(
    allocation: BetaAllocation,
    timing: dict[str, float],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        make_campaign(allocation, **timing)


def test_campaign_timing_is_identity_bound_and_round_trips(tmp_path: Path, allocation: BetaAllocation) -> None:
    default = make_campaign(allocation)
    timed = make_campaign(
        allocation,
        hold_min=3,
        hold_max=8,
        round_gap_min=2,
        round_gap_max=6,
    )
    assert timed.campaign_id != default.campaign_id

    store = BetaVolumeCampaignStore(tmp_path)
    store.create(timed)
    restored = store.load(timed.campaign_id).campaign

    assert restored == timed
    assert restored.hold_min_seconds == 3
    assert restored.hold_max_seconds == 8
    assert restored.round_gap_min_seconds == 2
    assert restored.round_gap_max_seconds == 6


def test_campaign_store_loads_legacy_schema_one_timing(tmp_path: Path, allocation: BetaAllocation) -> None:
    current = make_campaign(allocation)
    legacy = replace(
        current,
        schema_version=1,
        campaign_id="",
        cooldown_seconds=2.5,
        hold_min_seconds=0,
        hold_max_seconds=0,
        round_gap_min_seconds=2.5,
        round_gap_max_seconds=2.5,
    )._with_computed_id()
    campaign_payload = legacy.as_dict()
    for key in (
        "hold_min_seconds",
        "hold_max_seconds",
        "round_gap_min_seconds",
        "round_gap_max_seconds",
    ):
        campaign_payload.pop(key)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / f"{legacy.campaign_id}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state": "completed",
                "campaign": campaign_payload,
                "result": {"status": "completed"},
            }
        ),
        encoding="utf-8",
    )

    record = BetaVolumeCampaignStore(tmp_path).load(legacy.campaign_id)

    assert record.state == "completed"
    assert record.campaign.schema_version == 1
    assert record.campaign.hold_min_seconds == 0
    assert record.campaign.hold_max_seconds == 0
    assert record.campaign.round_gap_min_seconds == 2.5
    assert record.campaign.round_gap_max_seconds == 2.5


def test_campaign_uses_one_authorization_for_twenty_bounded_children(
    tmp_path: Path,
    allocation: BetaAllocation,
) -> None:
    calls = 0

    def execute_child(plan) -> dict:
        nonlocal calls
        calls += 1
        if calls < 20:
            return authoritative_result(
                "0",
                status="stopped",
                reason="empty_round_limit_exhausted",
            )
        return authoritative_result("200")

    campaign = make_campaign(allocation, max_runs=20)
    campaign_store = BetaVolumeCampaignStore(tmp_path / "campaigns")
    campaign_store.create(campaign)
    runner = service(tmp_path, allocation, execute_child)

    result = runner.execute(campaign)

    assert result["status"] == "completed"
    assert result["executed_quote_volume"] == "200"
    assert result["runs_used"] == 20
    assert calls == 20
    assert len(list((tmp_path / "children").glob("wv-*.json"))) == 20


def test_campaign_retries_only_read_failures_before_child_claim(tmp_path: Path, allocation: BetaAllocation) -> None:
    calls = 0
    delays: list[float] = []

    def execute_child(plan) -> dict:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise ccxt.NetworkError("preflight read failed")
        return authoritative_result("200")

    campaign = make_campaign(allocation)
    campaign_store = BetaVolumeCampaignStore(tmp_path / "campaigns")
    campaign_store.create(campaign)
    runner = service(tmp_path, allocation, execute_child, delays=delays)

    result = runner.execute(campaign)

    assert result["status"] == "completed"
    assert calls == 3
    assert delays == [1, 2]


def test_campaign_child_planning_recovers_from_transient_market_reads(
    tmp_path: Path,
    allocation: BetaAllocation,
) -> None:
    campaign = make_campaign(allocation)
    campaign_store = BetaVolumeCampaignStore(tmp_path / "campaigns")
    campaign_store.create(campaign)
    gateway = FlakyPlanningGateway(failures=2)
    delays: list[float] = []
    runner = service(
        tmp_path,
        allocation,
        lambda plan: authoritative_result("200"),
        gateway=gateway,
        delays=delays,
    )

    result = runner.execute(campaign)

    assert result["status"] == "completed"
    assert gateway.order_book_calls == 4
    assert delays == [1, 2]


def test_uncertain_child_hard_stops_without_another_submission(tmp_path: Path, allocation: BetaAllocation) -> None:
    calls = 0

    def execute_child(plan) -> dict:
        nonlocal calls
        calls += 1
        return authoritative_result("0", status="uncertain", reason="submission_uncertain")

    campaign = make_campaign(allocation)
    campaign_store = BetaVolumeCampaignStore(tmp_path / "campaigns")
    campaign_store.create(campaign)
    runner = service(tmp_path, allocation, execute_child)

    result = runner.execute(campaign)

    assert result["status"] == "uncertain"
    assert result["reason"] == "submission_uncertain"
    assert calls == 1


def test_uncertain_child_cannot_become_completed_by_reaching_target(
    tmp_path: Path,
    allocation: BetaAllocation,
) -> None:
    campaign = make_campaign(allocation)
    campaign_store = BetaVolumeCampaignStore(tmp_path / "campaigns")
    campaign_store.create(campaign)
    runner = service(
        tmp_path,
        allocation,
        lambda plan: authoritative_result("200", status="uncertain", reason="submission_uncertain"),
    )

    result = runner.execute(campaign)

    assert result["status"] == "uncertain"
    assert result["reason"] == "submission_uncertain"
    assert result["executed_quote_volume"] == "200"


def test_non_maker_child_is_never_counted_or_retried(tmp_path: Path, allocation: BetaAllocation) -> None:
    calls = 0

    def execute_child(plan) -> dict:
        nonlocal calls
        calls += 1
        result = authoritative_result("200", status="stopped", reason="taker_fill_detected")
        result["accounting"]["maker_only"] = False
        result["accounting"]["maker_count"] = 3
        result["accounting"]["taker_count"] = 1
        return result

    campaign = make_campaign(allocation)
    campaign_store = BetaVolumeCampaignStore(tmp_path / "campaigns")
    campaign_store.create(campaign)
    runner = service(tmp_path, allocation, execute_child)

    result = runner.execute(campaign)

    assert result["status"] == "stopped"
    assert result["executed_quote_volume"] == "0"
    assert result["reason"] == "child_accounting_not_verified_pure_maker"
    assert calls == 1


def test_invalid_child_volume_is_checkpointed_as_stopped(tmp_path: Path, allocation: BetaAllocation) -> None:
    campaign = make_campaign(allocation)
    campaign_store = BetaVolumeCampaignStore(tmp_path / "campaigns")
    campaign_store.create(campaign)
    runner = service(
        tmp_path,
        allocation,
        lambda plan: {"status": "completed", "executed_quote_volume": "NaN"},
    )

    result = runner.execute(campaign)

    assert result["status"] == "stopped"
    assert result["executed_quote_volume"] == "0"
    assert result["maker_only"] is False
    assert campaign_store.load(campaign.campaign_id).state == "stopped"


def test_campaign_is_bound_to_the_reviewed_profile(tmp_path: Path, allocation: BetaAllocation) -> None:
    campaign = make_campaign(allocation)
    campaign_store = BetaVolumeCampaignStore(tmp_path / "campaigns")
    campaign_store.create(campaign)
    runner = service(tmp_path, allocation, lambda plan: authoritative_result("200"), fingerprint="other-profile-123")

    with pytest.raises(SafetyError, match="different live profile"):
        runner.execute(campaign)

    assert campaign_store.load(campaign.campaign_id).state == "planned"


def test_unobservable_post_child_boundary_is_uncertain(tmp_path: Path, allocation: BetaAllocation) -> None:
    campaign = make_campaign(allocation)
    campaign_store = BetaVolumeCampaignStore(tmp_path / "campaigns")
    campaign_store.create(campaign)
    runner = service(
        tmp_path,
        allocation,
        lambda plan: authoritative_result("200"),
        gateway=BoundaryNetworkGateway(),
    )

    result = runner.execute(campaign)

    assert result["status"] == "uncertain"
    assert result["reason"] == "child_boundary_observation_unavailable"


def test_cli_uses_one_short_confirmation_and_keeps_live_environment_gate(
    tmp_path: Path,
    allocation: BetaAllocation,
    monkeypatch,
) -> None:
    settings = Settings.load(
        environ={
            "WEEX_API_KEY": "key",
            "WEEX_API_SECRET": "secret",
            "WEEX_API_PASSPHRASE": "pass",
            "WEEX_LIVE_TRADING_ENABLED": "false",
        }
    )
    profile = LiveProfile(
        path=tmp_path / "live.toml",
        settings=settings,
        proxy_url=None,
        allow_live_mutations=True,
        post_only_only=True,
    )
    monkeypatch.setattr(live, "profile_for", lambda ctx: profile)
    monkeypatch.setattr(live, "settings_for", lambda ctx: settings)
    monkeypatch.setattr(live, "gateway_for", lambda ctx: Gateway())
    monkeypatch.setattr(live, "HttpBetaAllocationProvider", lambda url: Provider(allocation))
    campaign_directory = tmp_path / "campaigns"
    child_directory = tmp_path / "children"

    planned = runner.invoke(
        app,
        [
            "--profile",
            str(profile.path),
            "live",
            "beta-campaign",
            "--target",
            "200",
            "--cycle-volume",
            "200",
            "--hold-min",
            "5",
            "--hold-max",
            "7",
            "--round-gap-min",
            "5",
            "--round-gap-max",
            "7",
            "--campaign-directory",
            str(campaign_directory),
            "--child-plan-directory",
            str(child_directory),
            "--json",
        ],
    )

    assert planned.exit_code == 0, planned.output
    payload = json.loads(planned.output)
    assert payload["confirm"].startswith("EXECUTE WEEX LIVE BETA-CAMPAIGN WC-")
    assert payload["confirm"].endswith("RUNS_20 POST_ONLY")
    assert payload["execute_command"].count("--confirm") == 1
    assert "--campaign" not in payload["execute_command"]
    assert payload["timing"] == {
        "hold_seconds": [300.0, 420.0],
        "round_gap_seconds": [300.0, 420.0],
        "selection": "uniform_per_cycle",
    }
    assert payload["campaign"]["hold_min_seconds"] == 300.0
    assert payload["campaign"]["hold_max_seconds"] == 420.0
    assert payload["campaign"]["round_gap_min_seconds"] == 300.0
    assert payload["campaign"]["round_gap_max_seconds"] == 420.0

    executed = runner.invoke(
        app,
        [
            "--profile",
            str(profile.path),
            "live",
            "beta-campaign",
            "--campaign-directory",
            str(campaign_directory),
            "--child-plan-directory",
            str(child_directory),
            "--execute",
            "--confirm",
            payload["confirm"],
            "--json",
        ],
    )

    assert executed.exit_code == 1
    assert "实盘交易未启用" in executed.output

    mistaken = runner.invoke(
        app,
        [
            "--profile",
            str(profile.path),
            "live",
            "beta-campaign",
            "--confirm",
            payload["confirm"],
            "--campaign-directory",
            str(campaign_directory),
            "--child-plan-directory",
            str(child_directory),
            "--json",
        ],
    )
    assert mistaken.exit_code == 2
    assert "--confirm 只能与 --execute 一起使用" in strip_ansi(mistaken.output)
    assert len(list(campaign_directory.glob("wc-*.json"))) == 1


def test_campaign_confirmation_contains_and_recovers_campaign_id(allocation: BetaAllocation) -> None:
    campaign = make_campaign(allocation)
    confirmation = campaign_confirmation(campaign)

    assert campaign_id_from_confirmation(confirmation) == campaign.campaign_id
    with pytest.raises(ValidationError, match="confirmation phrase"):
        campaign_id_from_confirmation(f"{confirmation} ")


def test_campaign_help_exposes_only_user_facing_strategy_controls() -> None:
    result = runner.invoke(app, ["live", "beta-campaign", "--help"])
    output = strip_ansi(result.output)

    assert result.exit_code == 0, result.output
    for option in (
        "--target",
        "--cycle-volume",
        "--hold-min",
        "--hold-max",
        "--round-gap-min",
        "--round-gap-max",
    ):
        assert option in output
    assert "--campaign" not in output
    assert "分钟" in output
    assert "秒数" not in output
    for removed in (
        "--max-position",
        "--timeout",
        "--recovery-attempts",
        "--max-empty-rounds",
        "--cooldown",
        "--max-runs",
        "--authorization-minutes",
        "--leverage",
    ):
        assert removed not in output


def test_campaign_rejects_ambiguous_round_option() -> None:
    result = runner.invoke(app, ["live", "beta-campaign", "--round", "300"])

    assert result.exit_code == 2
    assert "不存在该选项：--round" in strip_ansi(result.output)


def test_campaign_progress_events_are_visible() -> None:
    console = Console(record=True, width=120)

    render_execution_event(
        {
            "event": "campaign_run_started",
            "run": 2,
            "remaining_quote": "900",
            "child_plan_id": "wv-child",
        },
        console,
    )
    render_execution_event(
        {
            "event": "campaign_finished",
            "status": "completed",
            "total_quote": "3001.2",
            "reason": "campaign_target_completed",
        },
        console,
    )

    output = console.export_text()
    assert "Campaign 运行 2" in output
    assert "剩余 900 USDT" in output
    assert "Campaign 已完成" in output


def test_campaign_progress_explains_active_maker_waits() -> None:
    console = Console(record=True, width=160)

    render_execution_event(
        {
            "event": "leg_started",
            "round": 1,
            "sequence": 1,
            "symbol": "BTC",
            "action": "open",
            "side": "buy",
            "quantity": "0.002",
        },
        console,
    )
    render_execution_event(
        {
            "event": "leg_progress",
            "progress_event": "submit",
            "round": 1,
            "sequence": 1,
            "symbol": "BTC",
            "action": "open",
            "price": "70000",
            "quantity": "0.002",
        },
        console,
    )
    render_execution_event(
        {
            "event": "leg_progress",
            "progress_event": "wait",
            "waiting_for": "maker_fill",
            "round": 1,
            "sequence": 1,
            "symbol": "BTC",
            "action": "open",
            "elapsed_ms": 4_000,
            "remaining_ms": 116_000,
            "filled_quantity": "0.001",
            "order_quantity": "0.002",
        },
        console,
    )

    output = console.export_text()
    assert "准备开仓 BTC 买入" in output
    assert "Maker 挂单已提交" in output
    assert "等待 Maker 挂单成交" in output
    assert "已等待 4.0秒" in output
    assert "本单成交 0.001/0.002" in output


def test_campaign_result_renders_authoritative_volume() -> None:
    console = Console(record=True, width=120)

    rendered = render_human(
        {
            "kind": "beta_volume_campaign_execution",
            "status": "completed",
            "reason": "campaign_target_completed",
            "target_turnover_quote": "3000",
            "executed_quote_volume": "3003.6821",
            "remaining_quote": "0",
            "excess_quote": "3.6821",
            "maker_only": True,
            "runs_used": 1,
            "max_runs": 20,
            "elapsed_ms": 1_136_314,
            "final_boundary": {
                "active_position_count": 0,
                "regular_order_count": 0,
                "trigger_order_count": 0,
            },
        },
        console,
    )

    output = console.export_text()
    assert rendered is True
    assert "Beta Campaign · 已完成" in output
    assert "3003.6821 / 3000 USDT" in output
    assert "3.6821 USDT" in output


def test_campaign_dry_run_renders_campaign_details(allocation: BetaAllocation) -> None:
    campaign = make_campaign(
        allocation,
        hold_min=300,
        hold_max=420,
        round_gap_min=300,
        round_gap_max=420,
    )
    console = Console(record=True, width=120)

    rendered = render_human(
        {
            "kind": "beta_volume_campaign_plan",
            "status": "dry_run",
            "campaign": campaign.as_dict(),
            "confirm": campaign_confirmation(campaign),
            "execute_command": campaign_execute_command(campaign, Path("data/live-test/live.toml")),
        },
        console,
    )

    output = console.export_text()
    assert rendered is True
    assert "演练计划 · Beta Campaign" in output
    assert "目标成交量" in output
    assert "200 USDT" in output
    assert "开仓持有时间" in output and "5-7 分钟" in output
    assert "周期间隔" in output and "5-7 分钟" in output
    assert "每周期成交量" in output
    assert "执行命令" in output
    assert "--execute --confirm" in output
    assert "--campaign" not in output

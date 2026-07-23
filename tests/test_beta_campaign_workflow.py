from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest

import weex_cli.beta_campaign_workflow as workflow_module
from weex_cli.beta_allocation import BetaAllocation, BetaUnavailable
from weex_cli.beta_campaign import campaign_confirmation
from weex_cli.beta_campaign_workflow import (
    BetaCampaignApplication,
    CampaignPreviewRequest,
    CampaignRuntimePaths,
)
from weex_cli.config import Settings
from weex_cli.errors import SafetyError, ValidationError
from weex_cli.live_profile import LiveProfile


class Provider:
    def __init__(self, allocation: BetaAllocation) -> None:
        self.allocation = allocation

    def get(self) -> BetaAllocation:
        return self.allocation


class UnavailableProvider:
    def get(self) -> BetaAllocation:
        raise BetaUnavailable("beta_request_failed:timeout")


class Gateway:
    created: list[Gateway] = []

    def __init__(self, *, positions: int = 0) -> None:
        self.settings = Settings.load(
            environ={
                "WEEX_API_KEY": "key",
                "WEEX_API_SECRET": "secret",
                "WEEX_API_PASSPHRASE": "pass",
                "WEEX_LIVE_TRADING_ENABLED": "true",
            }
        )
        self.position_count = positions
        self.closed = False
        self.__class__.created.append(self)

    def fork(self) -> Gateway:
        return Gateway(positions=self.position_count)

    def close(self) -> None:
        self.closed = True

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
        return [{"contracts": "1"}] * self.position_count

    def open_orders(self, symbol: str | None = None, *, mode: str = "live", trigger: bool = False) -> list:
        return []

    def algo_orders(self, symbol: str | None = None) -> list:
        return []


class Runtime:
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def allocation() -> BetaAllocation:
    return BetaAllocation(
        beta=Decimal("0.5"),
        btc_long_weight=Decimal("0.6666666667"),
        eth_short_weight=Decimal("0.3333333333"),
        version="beta-v1:1000",
        as_of_ms=1000,
        confidence=Decimal("0.1"),
        confidence_threshold=Decimal("0.65"),
        source="test",
    )


def profile(tmp_path: Path, *, live_enabled: bool = True) -> LiveProfile:
    settings = Settings.load(
        environ={
            "WEEX_API_KEY": "key",
            "WEEX_API_SECRET": "secret",
            "WEEX_API_PASSPHRASE": "pass",
            "WEEX_LIVE_TRADING_ENABLED": str(live_enabled).lower(),
        }
    )
    return LiveProfile(
        path=tmp_path / "accounts.toml",
        settings=settings,
        proxy_url="http://user:password@127.0.0.1:8080",
        allow_live_mutations=True,
        post_only_only=True,
    )


def application(
    tmp_path: Path,
    allocation: BetaAllocation,
    *,
    gateway_factory=lambda: Gateway(),
    live_enabled: bool = True,
    runtime_factory=None,
) -> BetaCampaignApplication:
    return BetaCampaignApplication(
        profile(tmp_path, live_enabled=live_enabled),
        CampaignRuntimePaths(tmp_path / "campaigns", tmp_path / "plans"),
        gateway_factory=gateway_factory,
        provider_factory=lambda: Provider(allocation),  # type: ignore[arg-type]
        runtime_factory=runtime_factory,
        now_ms=lambda: 1000,
    )


def test_preview_uses_campaign_defaults_and_reports_balance(tmp_path: Path, allocation: BetaAllocation) -> None:
    app = application(tmp_path, allocation)

    payload = app.preview(CampaignPreviewRequest(), require_flat=True)

    assert payload["campaign"]["target_turnover_quote"] == "6000"
    assert payload["campaign"]["round_turnover_quote"] == "500"
    assert payload["campaign"]["leverage"] == "auto"
    assert payload["account_readiness"]["available_quote"] == "1000"
    assert payload["estimated_cycles"] == 12
    assert payload["confirm"].startswith("EXECUTE WEEX LIVE BETA-CAMPAIGN WC-")
    assert payload["stop_confirm"].startswith("STOP WEEX LIVE BETA-CAMPAIGN WC-")


def test_preview_rejects_nonflat_account(tmp_path: Path, allocation: BetaAllocation) -> None:
    app = application(tmp_path, allocation, gateway_factory=lambda: Gateway(positions=1))

    with pytest.raises(SafetyError, match="requires flat"):
        app.preview(CampaignPreviewRequest(), require_flat=True)


def test_account_snapshot_keeps_exchange_data_when_beta_is_unavailable(tmp_path: Path) -> None:
    gateway = Gateway()
    app = BetaCampaignApplication(
        profile(tmp_path),
        CampaignRuntimePaths(tmp_path / "campaigns", tmp_path / "plans"),
        gateway_factory=lambda: gateway,
        provider_factory=UnavailableProvider,  # type: ignore[arg-type]
    )

    snapshot = app.account_snapshot()

    assert snapshot["api_status"] == "ok"
    assert snapshot["available_quote"] == "1000"
    assert snapshot["active_position_count"] == 0
    assert snapshot["allocation_status"] == "unavailable"
    assert snapshot["allocation_error"] == "beta_request_failed:timeout"
    assert "allocation" not in snapshot
    assert gateway.closed is True


def test_execute_uses_four_distinct_gateways_and_closes_everything(
    tmp_path: Path,
    allocation: BetaAllocation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Gateway.created = []
    runtime = Runtime()
    observed: dict[str, object] = {}

    class Service:
        def __init__(self, gateway, provider, campaign_store, child_store, **kwargs) -> None:
            observed["gateway"] = gateway
            observed["lanes"] = kwargs["lane_gateways"]
            observed["market_data"] = kwargs["market_data"]

        def execute(self, campaign) -> dict[str, object]:
            return {"status": "completed", "campaign_id": campaign.campaign_id}

    monkeypatch.setattr(workflow_module, "LiveBetaVolumeCampaignService", Service)
    app = application(
        tmp_path,
        allocation,
        runtime_factory=lambda snapshot, live_profile: runtime,
    )
    preview = app.preview(CampaignPreviewRequest(target_quote="500", cycle_volume="500"), require_flat=True)
    Gateway.created = []

    result = app.execute(confirmation=str(preview["confirm"]))

    assert result["status"] == "completed"
    assert len(Gateway.created) == 4
    assert len({id(item) for item in Gateway.created}) == 4
    assert all(item.closed for item in Gateway.created)
    assert runtime.started is True and runtime.closed is True
    assert observed["market_data"] is runtime
    assert set(observed["lanes"]) == {"BTC", "ETH"}  # type: ignore[arg-type]


def test_execute_closes_all_resources_when_worker_raises(
    tmp_path: Path,
    allocation: BetaAllocation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Gateway.created = []
    runtime = Runtime()

    class Service:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def execute(self, campaign) -> dict[str, object]:
            raise RuntimeError("classified upstream")

    monkeypatch.setattr(workflow_module, "LiveBetaVolumeCampaignService", Service)
    app = application(tmp_path, allocation, runtime_factory=lambda snapshot, live_profile: runtime)
    preview = app.preview(CampaignPreviewRequest(target_quote="500", cycle_volume="500"), require_flat=True)
    Gateway.created = []

    with pytest.raises(RuntimeError, match="classified upstream"):
        app.execute(confirmation=str(preview["confirm"]))

    assert len(Gateway.created) == 4
    assert all(item.closed for item in Gateway.created)
    assert runtime.closed is True


def test_live_gate_is_checked_before_worker_resources(
    tmp_path: Path,
    allocation: BetaAllocation,
) -> None:
    calls = 0

    def factory() -> Gateway:
        nonlocal calls
        calls += 1
        return Gateway()

    planning = application(tmp_path, allocation, gateway_factory=factory, live_enabled=False)
    preview = planning.preview(CampaignPreviewRequest(target_quote="500", cycle_volume="500"))
    calls = 0

    with pytest.raises(SafetyError, match="live trading is disabled"):
        planning.execute(confirmation=str(preview["confirm"]))

    assert calls == 0


def test_interrupted_execution_becomes_terminal_uncertain(tmp_path: Path, allocation: BetaAllocation) -> None:
    app = application(tmp_path, allocation)
    preview = app.preview(CampaignPreviewRequest(target_quote="500", cycle_volume="500"))
    campaign_id = str(preview["campaign"]["campaign_id"])
    record = app.load(campaign_id)
    app.campaign_store.save(record.campaign, state="executing", result=None)

    assert app.mark_interrupted_uncertain() == [campaign_id]
    recovered = app.load(campaign_id)
    assert recovered.state == "uncertain"
    assert recovered.result["reason"] == "tui_process_restart"
    assert recovered.result["retry_allowed"] is False


def test_exact_confirmation_is_profile_bound(tmp_path: Path, allocation: BetaAllocation) -> None:
    app = application(tmp_path, allocation)
    preview = app.preview(CampaignPreviewRequest(target_quote="500", cycle_volume="500"))
    campaign_id = str(preview["campaign"]["campaign_id"])
    campaign = app.load(campaign_id).campaign

    with pytest.raises(ValidationError, match="confirmation"):
        app.execute(confirmation=campaign_confirmation(campaign) + " ")

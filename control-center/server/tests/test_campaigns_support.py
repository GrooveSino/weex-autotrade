


from decimal import Decimal
from pathlib import Path

from pydantic import SecretStr
from weex_cli.beta_allocation import BetaAllocation, BetaUnavailable
from weex_cli.beta_campaign import (
    BetaVolumeCampaign,
    campaign_confirmation,
)
from weex_cli.config import Credentials, Settings
from weex_cli.live_profile import LiveProfile

from fleet_api.config import ControlPlaneSettings


class FakeGateway:
    def __init__(self, *, available: str = "1000", positions: bool = False) -> None:
        self.available = available
        self.positions_non_empty = positions
        self.children: list[FakeGateway] = []
        self.closed = False
        self.balance_reads = 0

    def order_book(self, symbol: str, _limit: int = 5) -> dict[str, object]:
        return {"bids": [["100", "10"]], "asks": [["101", "10"]] if symbol == "BTC" else [["101", "10"]]}

    def amount_step(self, _symbol: str) -> Decimal:
        return Decimal("0.001")

    def amount_to_precision(self, _symbol: str, amount: Decimal) -> Decimal:
        return amount.quantize(Decimal("0.001"))

    def account_balance_rows(self, _mode: str) -> list[dict[str, str]]:
        self.balance_reads += 1
        return [{"asset": "USDT", "availableBalance": self.available}]

    def positions(self, _mode: str, _symbol: str) -> list[dict[str, str]]:
        return [{"size": "1", "side": "long"}] if self.positions_non_empty else []

    def open_orders(self, _symbol: str, *, mode: str = "live") -> list[dict[str, str]]:
        return []

    def algo_orders(self, _symbol: str) -> list[dict[str, str]]:
        return []

    def fork(self) -> "FakeGateway":
        child = FakeGateway(available=self.available, positions=self.positions_non_empty)
        self.children.append(child)
        return child

    def close(self) -> None:
        self.closed = True

class FakeBetaProvider:
    def __init__(self, allocation: BetaAllocation) -> None:
        self.allocation = allocation

    def get(self) -> BetaAllocation:
        return self.allocation

class UnavailableBetaProvider:
    def get(self) -> BetaAllocation:
        raise BetaUnavailable("beta_request_failed:httperror")

def live_settings(tmp_path, *, workers: int = 1) -> ControlPlaneSettings:
    return ControlPlaneSettings(
        adapter="weex-live",
        storage="sqlite",
        master_key=SecretStr("not-used-by-memory-journal"),
        live_campaigns_enabled=True,
        live_trading_enabled=True,
        live_campaign_worker_count=workers,
        campaign_data_directory=tmp_path / "campaign-data",
    )

def live_profile(tmp_path: Path) -> LiveProfile:
    return LiveProfile(
        path=tmp_path / "profile.toml",
        settings=Settings(
            credentials=Credentials("key", "secret", "passphrase"),
            default_mode="live",
            live_trading_enabled=True,
        ),
        proxy_url="https://user:password@example.test:443",
        allow_live_mutations=True,
        post_only_only=True,
    )

def sample_campaign() -> BetaVolumeCampaign:
    btc_weight = Decimal(1) / (Decimal(1) + Decimal("0.4"))
    allocation = BetaAllocation(
        beta=Decimal("0.4"),
        btc_long_weight=btc_weight,
        eth_short_weight=Decimal(1) - btc_weight,
        version="beta-v1:1",
        as_of_ms=1,
        confidence=Decimal("0.5"),
        confidence_threshold=Decimal("0.65"),
        source="fake",
    )
    return BetaVolumeCampaign(
        schema_version=2,
        campaign_id="wc-ABCDEF1234",
        created_at_ms=1,
        expires_at_ms=10_000,
        profile_fingerprint="f" * 64,
        target_turnover_quote=Decimal("6000"),
        round_turnover_quote_min=Decimal("500"),
        round_turnover_quote=Decimal("500"),
        max_position_quote=Decimal("1200"),
        timeout_seconds=60,
        recovery_attempts=3,
        max_empty_rounds=3,
        cooldown_seconds=0.0,
        hold_min_seconds=300.0,
        hold_max_seconds=420.0,
        round_gap_min_seconds=300.0,
        round_gap_max_seconds=420.0,
        max_runs=20,
        leverage="auto",
        max_auto_leverage=99,
        margin_buffer=Decimal("1.2"),
        margin_mode="isolated",
        allocation=allocation,
    )._with_computed_id()

def metadata(campaign: BetaVolumeCampaign) -> dict[str, object]:
    return {
        "confirmation": campaign_confirmation(campaign),
        "stop_confirmation": f"STOP WEEX LIVE BETA-CAMPAIGN {campaign.campaign_id.upper()} POST_ONLY",
        "available_quote": "100",
        "required_leverage": 6,
        "planned_leverage": 6,
        "max_supported_turnover_quote": "16500",
    }

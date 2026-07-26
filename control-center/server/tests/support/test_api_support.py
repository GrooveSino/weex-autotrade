import time
from concurrent.futures import Future
from decimal import Decimal

from fastapi.testclient import TestClient
from weex_cli.beta_campaign import BetaVolumeCampaign
from weex_cli.beta_campaign.allocation import BetaAllocation

from fleet_api.accounts.repository import InMemoryAccountRepository
from fleet_api.auth.vault import CredentialMaterial, EphemeralCredentialVault
from fleet_api.config.config import ControlPlaneSettings
from fleet_api.main import create_app


class LivePreviewGateway:
    def order_book(self, symbol: str, _limit: int = 5) -> dict[str, object]:
        return {"bids": [["100", "10"]], "asks": [["101", "10"]]}

    def amount_step(self, _symbol: str) -> Decimal:
        return Decimal("0.001")

    def amount_to_precision(self, _symbol: str, amount: Decimal) -> Decimal:
        return amount.quantize(Decimal("0.001"))

    def account_balance_rows(self, _mode: str) -> list[dict[str, str]]:
        return [{"asset": "USDT", "availableBalance": "1000"}]

    def positions(self, _mode: str, _symbol: str) -> list[dict[str, str]]:
        return []

    def open_orders(self, _symbol: str, *, mode: str = "live") -> list[dict[str, str]]:
        return []

    def algo_orders(self, _symbol: str) -> list[dict[str, str]]:
        return []

    def fork(self):
        return self

    def close(self) -> None:
        return None


class LivePreviewProvider:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def get(self):
        from weex_cli.beta_campaign.allocation import BetaAllocation

        return BetaAllocation(
            beta=Decimal("0.4"),
            btc_long_weight=Decimal("0.7142857142857142857142857143"),
            eth_short_weight=Decimal("0.2857142857142857142857142857"),
            version="fake-beta:1",
            as_of_ms=int(time.time() * 1000),
            confidence=Decimal("1"),
            confidence_threshold=Decimal("0"),
            source="fake",
        )


class UnavailableLivePreviewProvider:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def get(self):
        from weex_cli.beta_campaign.allocation import BetaUnavailable

        raise BetaUnavailable("beta_request_failed:httperror")


class HeldWorkerExecutor:
    """Accepts work without running it, so API lifecycle tests cannot place orders."""

    def __init__(self) -> None:
        self.submissions = 0

    def submit(self, *_args, **_kwargs) -> Future[None]:
        self.submissions += 1
        return Future()

    def shutdown(self, **_kwargs) -> None:
        return None


class ExpectedWriteFailure(RuntimeError):
    pass


class FailingCredentialVault(EphemeralCredentialVault):
    def put(self, instance_id: str, material: CredentialMaterial) -> None:
        raise ExpectedWriteFailure("vault write failed")


class FailingReplaceRepository(InMemoryAccountRepository):
    fail_next_replace = False

    def replace(self, instance):
        if self.fail_next_replace:
            self.fail_next_replace = False
            raise ExpectedWriteFailure("repository replace failed")
        return super().replace(instance)


class StaticBetaMarketProvider:
    async def market_snapshot(self) -> dict[str, object]:
        return {
            "schemaVersion": "1.0",
            "strategy": "btc_long_eth_short",
            "status": "low_confidence",
            "upstreamUsable": False,
            "reasonCodes": ["confidence_below_threshold"],
            "finalBeta": "0.44260456370165036",
            "btcLongRatio": "1.0",
            "ethShortRatio": "0.44260456370165036",
            "btcLongWeight": "0.6931906533236318",
            "ethShortWeight": "0.30680934667636806",
            "confidence": "0.60",
            "confidenceThreshold": "0.65",
            "source": "beta_v2",
            "asOfMs": 1784377856564,
            "generatedAtMs": 1784377856889,
            "ageMs": "324",
            "maxAgeMs": "10000",
        }


class RefreshTrackingBetaProvider:
    def __init__(self) -> None:
        self.refresh_calls = 0
        self.closed = False

    async def refresh(self) -> bool:
        self.refresh_calls += 1
        return True

    @staticmethod
    def seconds_until_refresh(maximum_seconds: float) -> float:
        return maximum_seconds

    async def aclose(self) -> None:
        self.closed = True


def client() -> TestClient:
    return TestClient(create_app(ControlPlaneSettings(seed_demo_data=False)))


def create_payload(*, mode: str = "demo") -> dict[str, object]:
    return {
        "name": "Test 01",
        "accountTag": "pytest",
        "mode": mode,
        "credentials": {
            "apiKey": "key-super-secret-ABCD",
            "apiSecret": "secret-never-return",
            "passphrase": "pass-never-return",
        },
        "proxy": {
            "type": "https",
            "url": "proxy-user:proxy-password@proxy.example.com:9341",
        },
    }


def strategy_payload(*, name: str = "20k shared", target: str = "20000") -> dict[str, object]:
    return {
        "name": name,
        "targetVolumeQuote": target,
        "roundTurnoverQuoteMin": "500",
        "roundTurnoverQuoteMax": "750",
        "positionHoldMinSeconds": 300,
        "positionHoldMaxSeconds": 900,
        "roundIntervalMinSeconds": 600,
        "roundIntervalMaxSeconds": 1800,
    }


def monitor_campaign(*, campaign_id: str, created_at_ms: int) -> BetaVolumeCampaign:
    return BetaVolumeCampaign(
        schema_version=3,
        campaign_id=campaign_id,
        created_at_ms=created_at_ms,
        expires_at_ms=created_at_ms + 60_000,
        profile_fingerprint="f" * 64,
        target_turnover_quote=Decimal("500"),
        round_turnover_quote_min=Decimal("40"),
        round_turnover_quote=Decimal("80"),
        max_position_quote=Decimal("120"),
        timeout_seconds=60,
        recovery_attempts=3,
        max_empty_rounds=3,
        cooldown_seconds=0,
        hold_min_seconds=5,
        hold_max_seconds=5,
        round_gap_min_seconds=10,
        round_gap_max_seconds=10,
        max_runs=1,
        leverage=2,
        max_auto_leverage=10,
        margin_buffer=Decimal("1.2"),
        margin_mode="cross",
        allocation=BetaAllocation(
            beta=Decimal("0.4"),
            btc_long_weight=Decimal("0.7"),
            eth_short_weight=Decimal("0.3"),
            version="test-beta:1",
            as_of_ms=created_at_ms,
            confidence=Decimal("1"),
            confidence_threshold=Decimal("0"),
            source="fake",
        ),
    )

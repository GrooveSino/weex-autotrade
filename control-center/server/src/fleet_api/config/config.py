from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import SecretStr


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class ControlPlaneSettings:
    adapter: str = "mock"
    storage: str = "memory"
    sqlite_path: Path = Path("data/fleet-control.db")
    master_key: SecretStr | None = None
    seed_demo_data: bool = True
    cors_origins: tuple[str, ...] = ("http://127.0.0.1:4173", "http://localhost:4173")
    mock_tick_interval_seconds: float = 2.4
    weex_poll_interval_seconds: float = 30
    # Match the CLI's conservative live-read timeout. Five seconds is too
    # short for account-scoped WebShare proxies and turned transient latency
    # into a persistent degraded state.
    weex_request_timeout_ms: int = 15_000
    weex_history_lookback_days: int = 365
    weex_history_pages_per_poll: int = 1
    weex_history_active_fallback_seconds: float = 15
    weex_history_max_concurrency: int = 1
    max_parallel_polls: int = 12
    # Wallet, positions, and one bounded history page are independent private
    # reads. Leave room for their request timeouts without cancelling a valid
    # snapshot halfway through.
    account_poll_timeout_seconds: float = 50
    mock_cycle_total_quote: Decimal = Decimal("20")
    beta_ratio_url: str = "http://127.0.0.1:5888/api/v1/hedge-ratio"
    beta_ratio_timeout_seconds: float = 3
    beta_refresh_interval_seconds: float = 10
    beta_background_refresh_enabled: bool = False
    live_campaigns_enabled: bool = False
    live_trading_enabled: bool = False
    # Legacy blocking worker count. New admission is governed by the explicit
    # active-execution and normal-phase budgets below.
    live_campaign_worker_count: int = 200
    max_active_executions: int = 200
    normal_phase_max_concurrency: int = 20
    normal_phase_starts_per_second: float = 4
    normal_phase_proxy_gap_seconds: float = 5
    execution_io_normal_capacity: int = 64
    execution_io_emergency_capacity: int = 32
    # The async actor runtime owns new production Campaign lifecycles.  Tests
    # can keep the legacy facade selected while exercising historical fixtures.
    async_actor_runtime_enabled: bool = False
    # The REST fallback is the Fleet-scale default. A public/private socket
    # pair per task would turn 200 logical executions into 400 idle streams.
    live_campaign_websockets_enabled: bool = False
    # Empty intentionally means the executor host itself is the sole public
    # market-data egress. Account proxies never apply to this connection.
    shared_market_data_proxy_url: str | None = None
    execution_phase_gap_seconds: float = 5
    execution_phase_jitter_seconds: float = 15
    taker_dust_max_quote: Decimal = Decimal("10.00")
    campaign_data_directory: Path = Path("server/data/beta-campaigns")
    executor_socket: Path = Path("run/weex-fleet-executor.sock")
    # Local-console identity is intentionally separate from exchange
    # credentials. It is enabled by the production launch configuration and
    # remains opt-in for in-memory unit-test fixtures.
    local_user_auth_required: bool = False
    users_toml_path: Path = Path("~/Library/Application Support/WEEXFleet/users.toml")

    def __post_init__(self) -> None:
        if self.adapter not in {"mock", "weex-readonly", "weex-live"}:
            raise ValueError("FLEET_CONTROL_ADAPTER must be 'mock', 'weex-readonly', or 'weex-live'")
        if self.storage not in {"memory", "sqlite"}:
            raise ValueError("FLEET_STORAGE must be 'memory' or 'sqlite'")
        if self.storage == "sqlite" and self.master_key is None:
            raise ValueError("FLEET_MASTER_KEY is required when FLEET_STORAGE=sqlite")
        if self.adapter == "weex-live":
            if not self.live_campaigns_enabled:
                raise ValueError("FLEET_LIVE_CAMPAIGNS_ENABLED is required for the weex-live adapter")
            if not self.live_trading_enabled:
                raise ValueError("WEEX_LIVE_TRADING_ENABLED is required for the weex-live adapter")
            if self.storage != "sqlite":
                raise ValueError("weex-live requires FLEET_STORAGE=sqlite")
        if self.live_campaign_worker_count < 1:
            raise ValueError("FLEET_LIVE_CAMPAIGN_WORKERS must be at least 1")
        if self.max_active_executions < 1:
            raise ValueError("FLEET_MAX_ACTIVE_EXECUTIONS must be at least 1")
        if self.normal_phase_max_concurrency < 1:
            raise ValueError("FLEET_NORMAL_PHASE_MAX_CONCURRENCY must be at least 1")
        if self.normal_phase_starts_per_second <= 0:
            raise ValueError("FLEET_NORMAL_PHASE_STARTS_PER_SECOND must be greater than 0")
        if self.normal_phase_proxy_gap_seconds < 0:
            raise ValueError("FLEET_NORMAL_PHASE_PROXY_GAP_SECONDS cannot be negative")
        if self.execution_io_normal_capacity < 1 or self.execution_io_emergency_capacity < 1:
            raise ValueError("Fleet execution I/O capacities must be at least 1")
        if self.execution_phase_gap_seconds < 0 or self.execution_phase_jitter_seconds < 0:
            raise ValueError("execution phase pacing intervals cannot be negative")
        if not self.taker_dust_max_quote.is_finite() or self.taker_dust_max_quote <= 0:
            raise ValueError("FLEET_TAKER_DUST_MAX_QUOTE must be finite and positive")
        if self.mock_tick_interval_seconds < 0.25:
            raise ValueError("FLEET_MOCK_TICK_SECONDS must be at least 0.25")
        if self.weex_poll_interval_seconds < 5:
            raise ValueError("FLEET_WEEX_POLL_SECONDS must be at least 5")
        if self.weex_request_timeout_ms < 1_000:
            raise ValueError("FLEET_WEEX_REQUEST_TIMEOUT_MS must be at least 1000")
        if not 1 <= self.weex_history_lookback_days <= 365:
            raise ValueError("FLEET_WEEX_HISTORY_LOOKBACK_DAYS must be between 1 and 365")
        if self.weex_history_pages_per_poll < 1:
            raise ValueError("FLEET_WEEX_HISTORY_PAGES_PER_POLL must be at least 1")
        if self.weex_history_active_fallback_seconds <= 0:
            raise ValueError("FLEET_WEEX_HISTORY_ACTIVE_FALLBACK_SECONDS must be greater than 0")
        if self.weex_history_max_concurrency < 1:
            raise ValueError("FLEET_WEEX_HISTORY_MAX_CONCURRENCY must be at least 1")
        if self.max_parallel_polls < 1:
            raise ValueError("FLEET_MAX_PARALLEL_POLLS must be at least 1")
        if self.account_poll_timeout_seconds <= 0:
            raise ValueError("FLEET_ACCOUNT_POLL_TIMEOUT_SECONDS must be greater than 0")
        if not self.mock_cycle_total_quote.is_finite() or self.mock_cycle_total_quote <= 0:
            raise ValueError("FLEET_MOCK_CYCLE_TOTAL_QUOTE must be finite and positive")
        parsed_ratio_url = urlsplit(self.beta_ratio_url)
        if parsed_ratio_url.scheme not in {"http", "https"} or not parsed_ratio_url.netloc:
            raise ValueError("FLEET_BETA_RATIO_URL must be an absolute HTTP(S) URL")
        if parsed_ratio_url.username is not None or parsed_ratio_url.password is not None:
            raise ValueError("FLEET_BETA_RATIO_URL must not contain credentials")
        if self.beta_ratio_timeout_seconds <= 0:
            raise ValueError("FLEET_BETA_RATIO_TIMEOUT_SECONDS must be greater than 0")
        if self.beta_refresh_interval_seconds <= 0:
            raise ValueError("FLEET_BETA_REFRESH_SECONDS must be greater than 0")
        if self.local_user_auth_required and not self.users_toml_path.expanduser().is_absolute():
            raise ValueError("FLEET_USERS_TOML must be an absolute path when local user authentication is enabled")

    @classmethod
    def load(cls) -> ControlPlaneSettings:
        origins = tuple(
            item.strip()
            for item in os.environ.get(
                "FLEET_CORS_ORIGINS",
                "http://127.0.0.1:4173,http://localhost:4173",
            ).split(",")
            if item.strip()
        )
        return cls(
            adapter=os.environ.get("FLEET_CONTROL_ADAPTER", "mock").strip().lower(),
            storage=os.environ.get("FLEET_STORAGE", "memory").strip().lower(),
            sqlite_path=Path(os.environ.get("FLEET_DB_PATH", "data/fleet-control.db")).expanduser(),
            master_key=(SecretStr(value) if (value := os.environ.get("FLEET_MASTER_KEY", "").strip()) else None),
            seed_demo_data=_as_bool(os.environ.get("FLEET_SEED_DEMO_DATA", "true")),
            cors_origins=origins,
            mock_tick_interval_seconds=float(os.environ.get("FLEET_MOCK_TICK_SECONDS", "2.4")),
            weex_poll_interval_seconds=float(os.environ.get("FLEET_WEEX_POLL_SECONDS", "15")),
            weex_request_timeout_ms=int(os.environ.get("FLEET_WEEX_REQUEST_TIMEOUT_MS", "5000")),
            weex_history_lookback_days=int(os.environ.get("FLEET_WEEX_HISTORY_LOOKBACK_DAYS", "365")),
            weex_history_pages_per_poll=int(os.environ.get("FLEET_WEEX_HISTORY_PAGES_PER_POLL", "1")),
            weex_history_active_fallback_seconds=float(
                os.environ.get("FLEET_WEEX_HISTORY_ACTIVE_FALLBACK_SECONDS", "15")
            ),
            weex_history_max_concurrency=int(os.environ.get("FLEET_WEEX_HISTORY_MAX_CONCURRENCY", "1")),
            max_parallel_polls=int(os.environ.get("FLEET_MAX_PARALLEL_POLLS", "12")),
            account_poll_timeout_seconds=float(os.environ.get("FLEET_ACCOUNT_POLL_TIMEOUT_SECONDS", "10")),
            mock_cycle_total_quote=Decimal(os.environ.get("FLEET_MOCK_CYCLE_TOTAL_QUOTE", "20")),
            beta_ratio_url=os.environ.get(
                "FLEET_BETA_RATIO_URL",
                "http://127.0.0.1:5888/api/v1/hedge-ratio",
            ).strip(),
            beta_ratio_timeout_seconds=float(os.environ.get("FLEET_BETA_RATIO_TIMEOUT_SECONDS", "3")),
            beta_refresh_interval_seconds=float(
                os.environ.get(
                    "FLEET_BETA_REFRESH_SECONDS",
                    os.environ.get("FLEET_BETA_RATIO_CACHE_SECONDS", "10"),
                )
            ),
            beta_background_refresh_enabled=_as_bool(os.environ.get("FLEET_BETA_BACKGROUND_REFRESH_ENABLED", "true")),
            live_campaigns_enabled=_as_bool(os.environ.get("FLEET_LIVE_CAMPAIGNS_ENABLED", "false")),
            live_trading_enabled=_as_bool(os.environ.get("WEEX_LIVE_TRADING_ENABLED", "false")),
            live_campaign_worker_count=int(os.environ.get("FLEET_LIVE_CAMPAIGN_WORKERS", "200")),
            max_active_executions=int(os.environ.get("FLEET_MAX_ACTIVE_EXECUTIONS", "200")),
            normal_phase_max_concurrency=int(os.environ.get("FLEET_NORMAL_PHASE_MAX_CONCURRENCY", "20")),
            normal_phase_starts_per_second=float(os.environ.get("FLEET_NORMAL_PHASE_STARTS_PER_SECOND", "4")),
            normal_phase_proxy_gap_seconds=float(os.environ.get("FLEET_NORMAL_PHASE_PROXY_GAP_SECONDS", "5")),
            execution_io_normal_capacity=int(os.environ.get("FLEET_EXECUTION_IO_NORMAL_CAPACITY", "64")),
            execution_io_emergency_capacity=int(os.environ.get("FLEET_EXECUTION_IO_EMERGENCY_CAPACITY", "32")),
            async_actor_runtime_enabled=_as_bool(os.environ.get("FLEET_ASYNC_ACTOR_RUNTIME_ENABLED", "true")),
            live_campaign_websockets_enabled=_as_bool(
                os.environ.get("FLEET_LIVE_CAMPAIGN_WEBSOCKETS_ENABLED", "false")
            ),
            shared_market_data_proxy_url=(os.environ.get("FLEET_SHARED_MARKET_DATA_PROXY_URL", "").strip() or None),
            execution_phase_gap_seconds=float(os.environ.get("FLEET_EXECUTION_PHASE_GAP_SECONDS", "5")),
            execution_phase_jitter_seconds=float(os.environ.get("FLEET_EXECUTION_PHASE_JITTER_SECONDS", "15")),
            taker_dust_max_quote=Decimal(os.environ.get("FLEET_TAKER_DUST_MAX_QUOTE", "10.00")),
            campaign_data_directory=Path(
                os.environ.get("FLEET_CAMPAIGN_DATA_DIR", "server/data/beta-campaigns")
            ).expanduser(),
            executor_socket=Path(os.environ.get("FLEET_EXECUTOR_SOCKET", "run/weex-fleet-executor.sock")).expanduser(),
            local_user_auth_required=_as_bool(os.environ.get("FLEET_LOCAL_USER_AUTH_REQUIRED", "true")),
            users_toml_path=Path(
                os.environ.get("FLEET_USERS_TOML", "~/Library/Application Support/WEEXFleet/users.toml")
            ).expanduser(),
        )

    @property
    def poll_interval_seconds(self) -> float:
        return self.mock_tick_interval_seconds if self.adapter == "mock" else self.weex_poll_interval_seconds

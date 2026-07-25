from __future__ import annotations

from decimal import Decimal

from fleet_api.accounts.repository import AccountRepository
from fleet_api.models import (
    AccountInstance,
    CycleSnapshot,
    ExposureSnapshot,
    InstanceStatus,
    LogLevel,
    LogLine,
    ProxySnapshot,
    ProxyStatus,
    ProxyType,
    StrategyProgress,
    TradingMode,
    VolumeSnapshot,
    VolumeStrategy,
    WalletSnapshot,
)
from fleet_api.volume.core.volume_history import NormalizedTradeFill, TradeVolumeLedger, shanghai_day_start_ms


def seed_mock_instances(
    repository: AccountRepository,
    volume_ledger: TradeVolumeLedger,
    now_ms: int,
    mock_cycle_total_quote: Decimal = Decimal("20"),
) -> None:
    shared_strategy = VolumeStrategy(
        id="strategy-api-shared",
        name="标准双币成交量策略",
        target_volume_quote=Decimal("20000"),
        round_turnover_quote_min=Decimal("500"),
        round_turnover_quote_max=Decimal("750"),
        position_hold_min_seconds=300,
        position_hold_max_seconds=900,
        round_interval_min_seconds=600,
        round_interval_max_seconds=1800,
    )
    if repository.get_strategy(shared_strategy.id) is None:
        repository.create_strategy(shared_strategy)
    rows = [
        AccountInstance(
            id="ins-api-01",
            name="API Alpha 01",
            account_tag="控制平面",
            api_key_tail="8F2A",
            mode=TradingMode.DEMO,
            status=InstanceStatus.RUNNING,
            phase="Mock 周期运行中",
            proxy=ProxySnapshot(
                type=ProxyType.HTTPS,
                host="proxy.example.com:9341",
                location="US / New York",
                latency_ms=86,
                status=ProxyStatus.HEALTHY,
            ),
            wallet=WalletSnapshot(equity=12582.41, available=10428.18, unrealized_pnl=18.42),
            volume=VolumeSnapshot(lifetime=2847193.28, today=184206.14, complete=True),
            exposure=ExposureSnapshot(btc_long=1184.2, eth_short=1129.7),
            cycle=CycleSnapshot(completed=42, target=100, next_action_at="8s"),
            strategy_id=shared_strategy.id,
            strategy=shared_strategy,
            strategy_progress=StrategyProgress(generated_volume_quote=Decimal("12780")),
            mock_cycle_total_quote=mock_cycle_total_quote,
            updated_at="刚刚",
            unread_logs=1,
        ),
        AccountInstance(
            id="ins-api-02",
            name="API Beta 01",
            account_tag="控制平面",
            api_key_tail="E423",
            mode=TradingMode.DEMO,
            status=InstanceStatus.WARNING,
            phase="余额低于预警线",
            proxy=ProxySnapshot(
                type=ProxyType.SOCKS5,
                host="proxy.example.com:1080",
                location="SG / Singapore",
                latency_ms=221,
                status=ProxyStatus.DEGRADED,
            ),
            wallet=WalletSnapshot(equity=1864.72, available=824.1, unrealized_pnl=-16.24),
            volume=VolumeSnapshot(lifetime=894231.44, today=38218.04, complete=True),
            exposure=ExposureSnapshot(btc_long=486.3, eth_short=451.8),
            cycle=CycleSnapshot(completed=11, target=80),
            strategy_id=shared_strategy.id,
            strategy=shared_strategy,
            strategy_progress=StrategyProgress(generated_volume_quote=Decimal("2800")),
            mock_cycle_total_quote=mock_cycle_total_quote,
            updated_at="9 秒前",
            unread_logs=2,
        ),
    ]
    for row in rows:
        repository.create(row)
        repository.append_log(
            row.id,
            LogLine(
                id=f"{row.id}-seed",
                timestamp="2026-07-18T00:00:00+00:00",
                level=LogLevel.INFO,
                message="由模拟控制平面适配器初始化",
            ),
        )
    ensure_mock_volume_baselines(rows, volume_ledger, now_ms)


def ensure_mock_volume_baselines(
    instances: list[AccountInstance],
    volume_ledger: TradeVolumeLedger,
    now_ms: int,
) -> None:
    today_start = shanghai_day_start_ms(now_ms)
    for instance in instances:
        if volume_ledger.aggregate(instance.id, today_start).fill_count:
            continue
        historical = max(
            Decimal(0),
            Decimal(str(instance.volume.lifetime)) - Decimal(str(instance.volume.today)),
        )
        fills = tuple(
            fill
            for fill in (
                NormalizedTradeFill(
                    identity=f"mock-baseline:{instance.id}:historical",
                    executed_at_ms=max(0, today_start - 1),
                    quote_volume=historical,
                    symbol="ALL",
                ),
                NormalizedTradeFill(
                    identity=f"mock-baseline:{instance.id}:today",
                    executed_at_ms=today_start,
                    quote_volume=Decimal(str(instance.volume.today)),
                    symbol="ALL",
                ),
            )
            if fill.quote_volume > 0
        )
        if fills:
            volume_ledger.record(instance.id, fills)
        volume_ledger.set_complete(instance.id, instance.volume.complete)

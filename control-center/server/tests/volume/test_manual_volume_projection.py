from __future__ import annotations

import asyncio
from decimal import Decimal

from fleet_api.models import AccountTradeVolumeProjection
from fleet_api.volume.core.volume_history import InMemoryTradeVolumeLedger

from .test_account_trade_volume_report import (
    NOW_MS,
    AuthoritativeReader,
    account,
    fill,
    material,
    report_service,
)


def test_report_returns_the_committed_account_volume_projection() -> None:
    async def scenario() -> None:
        ledger = InMemoryTradeVolumeLedger()
        reader = AuthoritativeReader((fill("seven-day", 2, "20"), fill("thirty-day", 20, "30")))
        service = report_service(reader, ledger)

        weekly = await service.report(account(), material(), [7])
        monthly = await service.report(account(), material(), [30])
        repeated = await service.report(account(), material(), [30])

        assert weekly.account_volume == AccountTradeVolumeProjection(
            lifetime_quote_volume=Decimal("20"),
            today_quote_volume=Decimal(0),
            source_complete=False,
        )
        assert monthly.account_volume.lifetime_quote_volume == Decimal("50")
        assert repeated.ledger_inserted_fill_count == 0
        assert repeated.account_volume == monthly.account_volume
        assert ledger.aggregate("ins-volume", NOW_MS).lifetime == Decimal("50")

    asyncio.run(scenario())

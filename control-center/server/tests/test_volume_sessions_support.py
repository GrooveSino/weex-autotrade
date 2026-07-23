
from decimal import Decimal

from fleet_api.volume_history import (
    NormalizedTradeFill,
)


def fill(fill_id: str, quote: str, action: str, *, authoritative: bool = True, maker: bool | None = True):
    return NormalizedTradeFill(
        identity=fill_id,
        executed_at_ms=1_000,
        quote_volume=Decimal(quote),
        symbol="BTCUSDT",
        order_id=f"order-{fill_id}",
        base_quantity=Decimal("0.001"),
        position_action=action,
        maker=maker,
        authoritative=authoritative,
    )

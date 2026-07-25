from __future__ import annotations

import sqlite3
from decimal import Decimal

from fleet_api.volume.core.volume_contracts import NormalizedTradeFill, VolumeSession


def row_to_session(row: sqlite3.Row) -> VolumeSession:
    return VolumeSession(
        session_id=str(row["session_id"]),
        account_id=str(row["account_id"]),
        mode=str(row["mode"]),
        started_at_ms=int(row["started_at_ms"]),
        target_quote_volume=Decimal(str(row["target_quote_volume"])),
        status=str(row["status"]),
        verified_quote_volume=Decimal(str(row["verified_quote_volume"])),
        remaining_quote_volume=Decimal(str(row["remaining_quote_volume"])),
        last_sync_at_ms=None if row["last_sync_at_ms"] is None else int(row["last_sync_at_ms"]),
        last_reconciliation_at_ms=(
            None if row["last_reconciliation_at_ms"] is None else int(row["last_reconciliation_at_ms"])
        ),
        source_complete=bool(row["source_complete"]),
        stale=bool(row["stale"]),
        reconciliation_required=bool(row["reconciliation_required"]),
        discrepancy_quote_volume=Decimal(str(row["discrepancy_quote_volume"])),
        cursor=row["cursor"],
        high_watermark_ms=row["high_watermark_ms"],
        pending_sync=bool(row["pending_sync"]),
        maker_only_required=bool(row["maker_only_required"]),
        uncertain_order_state=bool(row["uncertain_order_state"]),
        audit_status=str(row["audit_status"] or "pending"),
        strategy_id=row["strategy_id"],
        strategy_name=row["strategy_name"],
        strategy_version=None if row["strategy_version"] is None else int(row["strategy_version"]),
        direction=str(row["direction"] or "btc_long_eth_short"),
        target_mode=str(row["target_mode"] or "incremental"),
        strategy_target_quote_volume=Decimal(str(row["strategy_target_quote_volume"] or row["target_quote_volume"])),
        baseline_lifetime_quote_volume=Decimal(str(row["baseline_lifetime_quote_volume"] or "0")),
        finished_at_ms=None if row["finished_at_ms"] is None else int(row["finished_at_ms"]),
        result=row["result"],
        result_reason=row["result_reason"],
        final_lifetime_quote_volume=(
            None if row["final_lifetime_quote_volume"] is None else Decimal(str(row["final_lifetime_quote_volume"]))
        ),
        starting_available_balance_quote=(
            None
            if row["starting_available_balance_quote"] is None
            else Decimal(str(row["starting_available_balance_quote"]))
        ),
        ending_available_balance_quote=(
            None
            if row["ending_available_balance_quote"] is None
            else Decimal(str(row["ending_available_balance_quote"]))
        ),
    )


def row_to_fill(row: sqlite3.Row) -> NormalizedTradeFill:
    return NormalizedTradeFill(
        identity=str(row[0]).split(":", 1)[-1],
        executed_at_ms=int(row[1]),
        quote_volume=Decimal(str(row[2])),
        symbol=str(row[3]),
        order_id=str(row[4]),
        base_quantity=Decimal(str(row[5])),
        side=str(row[6]),
        position_side=str(row[7]),
        position_action=str(row[8]),
        maker=None if row[9] is None else bool(row[9]),
        commission=Decimal(str(row[10])),
        commission_asset=row[11],
        realized_pnl=Decimal(str(row[12])),
        source=str(row[13]),
        authoritative=bool(row[14]),
        created_at_ms=int(row[15]),
    )

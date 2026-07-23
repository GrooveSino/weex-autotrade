from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Protocol

from weex_cli.beta_campaign import inspect_live_account, live_profile_fingerprint
from weex_cli.gateway import WeexGateway
from weex_cli.live_profile import LiveProfile

from .service import UnsafeOperation
from .vault import CredentialMaterial


class RecoveryJournal(Protocol):
    def list_for_instance(self, instance_id: str) -> list[Any]: ...

    def update(self, campaign_id: str, **metadata: Any) -> None: ...


ProfileGatewayFactory = Callable[[CredentialMaterial], tuple[LiveProfile, WeexGateway]]
EventAppender = Callable[[Any, dict[str, Any]], int]
EventSanitizer = Callable[[dict[str, Any]], dict[str, Any]]
Notifier = Callable[[str], None]
FlatBoundaryCheck = Callable[[dict[str, Any]], bool]
ReconciliationRequired = Callable[[Any], bool]


def recover_uncertain_before_preview(
    journal: RecoveryJournal,
    instance_id: str,
    material: CredentialMaterial,
    *,
    profile_and_gateway: ProfileGatewayFactory,
    reconciliation_required: ReconciliationRequired,
    account_boundary_is_flat: FlatBoundaryCheck,
    append_monitor_event: EventAppender,
    sanitize_event: EventSanitizer,
    notify: Notifier,
) -> None:
    """Acknowledge historical uncertainty only after a fresh read-only check."""
    record = next((item for item in journal.list_for_instance(instance_id) if reconciliation_required(item)), None)
    if record is None:
        return
    acknowledge_recovered_uncertain(
        journal,
        record,
        material,
        source="automatic_preview",
        profile_and_gateway=profile_and_gateway,
        account_boundary_is_flat=account_boundary_is_flat,
        append_monitor_event=append_monitor_event,
        sanitize_event=sanitize_event,
        notify=notify,
    )


def acknowledge_recovered_uncertain(
    journal: RecoveryJournal,
    record: Any,
    material: CredentialMaterial,
    *,
    source: str,
    profile_and_gateway: ProfileGatewayFactory,
    account_boundary_is_flat: FlatBoundaryCheck,
    append_monitor_event: EventAppender,
    sanitize_event: EventSanitizer,
    notify: Notifier,
) -> None:
    """Persist a recovery acknowledgement without issuing any exchange mutation."""
    gateway: WeexGateway | None = None
    try:
        profile, gateway = profile_and_gateway(material)
        if live_profile_fingerprint(profile) != record.campaign.profile_fingerprint:
            raise UnsafeOperation("previous execution cannot be recovered because the Live profile changed")
        boundary = inspect_live_account(gateway, Decimal(0))
        if not account_boundary_is_flat(boundary):
            raise UnsafeOperation(_recovery_blocker(boundary))
    finally:
        if gateway is not None:
            gateway.close()

    journal.update(
        record.campaign_id,
        reconciliation_acknowledged_at_ms=int(time.time() * 1000),
        reconciliation_boundary="btc_eth_flat_no_regular_or_trigger_orders",
        reconciliation_source=source,
    )
    name = "campaign_reconciliation_acknowledged" if source == "manual" else "campaign_recovery_verified"
    event = sanitize_event({"event": name})
    event["sequence"] = append_monitor_event(record, event)
    notify(record.instance_id)


def _recovery_blocker(boundary: dict[str, Any]) -> str:
    positions = int(boundary.get("active_position_count") or 0)
    regular_orders = int(boundary.get("regular_order_count") or 0)
    trigger_orders = int(boundary.get("trigger_order_count") or 0)
    details = [
        f"BTC/ETH 持仓 {positions} 个" if positions else "",
        f"普通挂单 {regular_orders} 个" if regular_orders else "",
        f"条件单 {trigger_orders} 个" if trigger_orders else "",
    ]
    present = "、".join(item for item in details if item)
    return f"previous execution is still blocking launch: {present or '账户边界未通过'}"

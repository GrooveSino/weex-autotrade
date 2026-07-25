"""Read-only launch boundary inspection and one-shot order cancellation."""

from __future__ import annotations

import time

from weex_cli.gateway import WeexGateway
from weex_cli.reliability import NETWORK_ERRORS

from fleet_api.accounts.account_boundary_reader import AccountBoundaryReader
from fleet_api.auth.vault import CredentialMaterial
from fleet_api.campaigns.core.campaign_helpers import _cleanup_confirmation
from fleet_api.models import BetaCampaignStatus
from fleet_api.services.control.service import UnsafeOperation

_VERIFY_ATTEMPTS = 5


class CampaignCleanupMixin:
    def cleanup_bound_strategy(
        self,
        instance_id: str,
        confirmation: str,
        material: CredentialMaterial | None,
    ) -> dict[str, object]:
        """Cancel regular and trigger orders once; never flatten launch-time positions."""
        self._require_live_gate()
        if confirmation != _cleanup_confirmation(instance_id):
            raise UnsafeOperation("撤单确认短语不匹配")
        if material is None:
            raise UnsafeOperation("账号凭据不可用")
        with self._lock:
            if instance_id in self._cleaning:
                raise UnsafeOperation("该账号的启动前撤单正在执行")
            self._cleaning.add(instance_id)
        gateway: WeexGateway | None = None
        try:
            _, gateway = self._profile_and_gateway(material)
            cancellation_errors: list[str] = []
            for symbol in ("BTC", "ETH"):
                for trigger in (False, True):
                    try:
                        gateway.cancel_all_orders(symbol, mode="live", trigger=trigger)
                    except NETWORK_ERRORS:
                        # The request may have landed. Never repeat it; verify below.
                        continue
                    except Exception as exc:  # noqa: BLE001 - return a non-sensitive structured reason
                        kind = "条件单" if trigger else "普通单"
                        cancellation_errors.append(f"{symbol} {kind}撤销失败：{type(exc).__name__}")
            boundary = self._verify_orders_cleared(gateway)
            verified = not boundary["regular_order_count"] and not boundary["trigger_order_count"]
            if not verified:
                raise UnsafeOperation(
                    "撤单结果尚未核验：仍有 "
                    f"{boundary['regular_order_count']} 个普通单、{boundary['trigger_order_count']} 个条件单"
                )
            if cancellation_errors:
                boundary["warnings"] = cancellation_errors
            return {"verified": True, **boundary}
        finally:
            if gateway is not None:
                gateway.close()
            with self._lock:
                self._cleaning.discard(instance_id)

    def _verify_orders_cleared(self, gateway: WeexGateway) -> dict[str, object]:
        last: dict[str, object] | None = None
        for attempt in range(_VERIFY_ATTEMPTS):
            try:
                last = self._read_public_boundary(gateway)
            except NETWORK_ERRORS:
                last = None
            if last is not None and not last["regular_order_count"] and not last["trigger_order_count"]:
                return last
            if attempt + 1 < _VERIFY_ATTEMPTS:
                time.sleep(min(2.0, 0.25 * (2**attempt)))
        if last is None:
            raise UnsafeOperation("撤单后无法读取挂单状态，请稍后重新检查；系统不会自动重发撤单命令")
        return last

    def inspect_bound_strategy_boundary(self, material: CredentialMaterial) -> dict[str, object]:
        """Read positions and orders without changing lifecycle or exchange state."""
        gateway: WeexGateway | None = None
        try:
            _, gateway = self._profile_and_gateway(material)
            return self._read_public_boundary(gateway)
        finally:
            if gateway is not None:
                gateway.close()

    def _read_public_boundary(self, gateway: WeexGateway) -> dict[str, object]:
        with self._lock:
            reader = getattr(self, "_account_boundary_reader", None)
            if reader is None:
                reader = AccountBoundaryReader(
                    self.io_budget,
                    max_workers=min(self.settings.max_parallel_polls, self.settings.execution_io_normal_capacity),
                )
                self._account_boundary_reader = reader
        return reader.read(gateway)

    def close_boundary_reader(self) -> None:
        with self._lock:
            reader = getattr(self, "_account_boundary_reader", None)
            self._account_boundary_reader = None
        if reader is not None:
            reader.close()

    def archive_bound_strategy_recovery(self, record, *, recovered_at_ms: int) -> None:  # type: ignore[no-untyped-def]
        if record.status in {BetaCampaignStatus.EXECUTING.value, BetaCampaignStatus.STOPPING.value}:
            raise UnsafeOperation("旧任务仍在执行或停止中，不能创建新任务")
        updates = {
            "reconciliation_acknowledged_at_ms": recovered_at_ms,
            "reconciliation_boundary": "btc_eth_flat_no_regular_or_trigger_orders",
            "reconciliation_source": "automatic_startup_recovery",
        }
        if record.status in {"uncertain", "recovering", "planned"}:
            updates.update(status="stopped", finished_at_ms=recovered_at_ms, reason="automatic_startup_recovery")
        self.journal.update(record.campaign_id, **updates)
        self._notify(record.instance_id)

    def _require_live_gate(self) -> None:
        if (
            self.settings.adapter != "weex-live"
            or not self.settings.live_campaigns_enabled
            or not self.settings.live_trading_enabled
        ):
            raise UnsafeOperation("实盘 Campaign 执行器未启用 (disabled)")

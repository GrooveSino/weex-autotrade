"""Restart only flat, no-order condition waits as live Campaign actors."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from weex_cli.beta_volume import BetaVolumePlanStore

from fleet_api.campaigns.actors.campaign_actor_models import CampaignActorContext
from fleet_api.campaigns.core.campaign_contracts import CampaignRecord, _AccountLease
from fleet_api.campaigns.core.campaign_events import submission_attempted
from fleet_api.campaigns.manager.helpers.actor_values import summary_from_checkpoint
from fleet_api.models import BetaCampaignStatus


class CampaignRestartMixin:
    """Preserve automatic condition waits across a control-plane restart."""

    def recover(self) -> int:
        resumable = [record.campaign_id for record in self.journal.list_all() if _can_resume_condition_wait(record)]
        count = self.journal.recover_incomplete()
        for campaign_id in resumable:
            record = self.journal.get(campaign_id)
            if record is not None:
                self._resume_condition_wait(record)
        for record in self.journal.list_all():
            self._notify(record.instance_id)
        return count

    def _resume_condition_wait(self, record: CampaignRecord) -> None:
        material = self.vault.get(record.instance_id)
        if material is None or not self.capacity.admit(record.campaign_id):
            return
        lease = _AccountLease(
            self.settings.campaign_data_directory,
            material.api_key.get_secret_value(),
            record.instance_id,
            record.campaign_id,
        )
        stop = threading.Event()
        try:
            lease.acquire()
            context = _resume_context(record, self.settings.campaign_data_directory)
            with self._lock:
                if self._closing or record.campaign_id in self._actor_futures:
                    lease.release()
                    self.capacity.release_execution(record.campaign_id)
                    return
                self._stops[record.campaign_id] = stop
                self._leases[record.campaign_id] = lease
            self.journal.update(
                record.campaign_id,
                status=BetaCampaignStatus.EXECUTING.value,
                phase="condition_waiting",
                restart_rehydrated=True,
                recovery_state=None,
                next_recovery_check_at_ms=None,
            )
            self._start_actor(record, material, stop, resume_context=context)
        except Exception:
            with self._lock:
                self._stops.pop(record.campaign_id, None)
                self._leases.pop(record.campaign_id, None)
            lease.release()
            self.capacity.release_execution(record.campaign_id)
            self.journal.update(
                record.campaign_id,
                status=BetaCampaignStatus.RECOVERING.value,
                reason="restart_condition_rehydrate_failed",
            )


def _can_resume_condition_wait(record: CampaignRecord) -> bool:
    if record.status != BetaCampaignStatus.EXECUTING.value or record.metadata.get("phase") != "condition_waiting":
        return False
    ownership = record.metadata.get("execution_ownership")
    if isinstance(ownership, Mapping):
        return ownership.get("state") in {"planned", "closed"}
    return not submission_attempted(record)


def _resume_context(record: CampaignRecord, root) -> CampaignActorContext | None:  # type: ignore[no-untyped-def]
    ownership = record.metadata.get("execution_ownership")
    if not isinstance(ownership, Mapping):
        return None
    plan_id = ownership.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id:
        return None
    try:
        child = BetaVolumePlanStore(root / record.instance_id / "plans").load_record(plan_id).plan
    except Exception:
        return None
    completed_quote = _completed_quote(record, ownership)
    return CampaignActorContext(
        child=child,
        run_number=1,
        execution_started_at_ms=_positive_int(record.metadata.get("started_at_ms"), record.campaign.created_at_ms),
        round_number=max(1, _positive_int(ownership.get("round"), 1)),
        child_total_quote=completed_quote,
        summaries=summary_from_checkpoint(ownership.get("accounting_checkpoint"), completed_quote),
        attempt_number=max(0, _positive_int(ownership.get("attempt"), 0)),
        condition_attempt=max(0, _positive_int(record.metadata.get("condition_attempt"), 0)),
        condition_code=_text_or_none(record.metadata.get("condition_state")),
    )


def _decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal(0)
    return parsed if parsed.is_finite() and parsed >= 0 else Decimal(0)


def _completed_quote(record: CampaignRecord, ownership: Mapping[str, object]) -> Decimal:
    stored = _decimal(ownership.get("completed_quote"))
    if stored > 0:
        return stored
    totals = []
    for event in record.events:
        fields = event.get("fields") if isinstance(event.get("fields"), Mapping) else {}
        if event.get("name") == "cycle_completed":
            totals.append(_decimal(fields.get("total_quote")))
    return max(totals, default=Decimal(0))


def _positive_int(value: object, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _text_or_none(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None

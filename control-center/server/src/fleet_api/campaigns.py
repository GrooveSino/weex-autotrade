"""Compatibility exports for the Fleet live-campaign subsystem."""

from weex_cli.beta_campaign import LiveBetaVolumeCampaignService
from weex_cli.live_websocket import WeexCampaignWebSocketRuntime

from .campaign_contracts import (
    CampaignJournal,
    CampaignRecord,
    ExecutionMonitorProjection,
    _AccountLease,
)
from .campaign_events import (
    _phase_for_event,
    _publishes_fleet_snapshot,
    _safe_event_text,
    _sanitize_event,
    _view,
)
from .campaign_helpers import (
    _account_boundary_is_flat,
    _available_quote,
    _available_quote_from_readiness,
    _bound_strategy_confirmation,
    _bound_strategy_stop_confirmation,
    _campaign_result_metrics,
    _decimal_field,
    _int_field,
    _normalize_proxy_url,
    _preview_metadata,
    _reconciliation_confirmation,
    _reconciliation_required,
    _worker_exception_reason,
)
from .campaign_manager import CampaignWorkerManager
from .campaign_memory_journal import InMemoryCampaignJournal
from .campaign_sqlite_base import SQLiteCampaignJournalBase
from .campaign_sqlite_monitor import SQLiteCampaignJournalMonitorMixin


class SQLiteCampaignJournal(SQLiteCampaignJournalBase, SQLiteCampaignJournalMonitorMixin):
    """SQLite journal composed from persistence and monitor-projection concerns."""


__all__ = [
    "CampaignJournal",
    "CampaignRecord",
    "CampaignWorkerManager",
    "ExecutionMonitorProjection",
    "InMemoryCampaignJournal",
    "SQLiteCampaignJournal",
]

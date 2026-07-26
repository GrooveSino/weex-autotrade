"""Application-facing contracts for the multi-account Campaign TUI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from weex_cli.beta_campaign.workflow import CampaignPreviewRequest, CampaignRuntimePaths

from .accounts import TuiAccount, TuiAccountCatalog


class CampaignWorkflow(Protocol):
    profile_fingerprint: str

    def account_snapshot(self) -> dict[str, Any]: ...

    def account_boundary(self) -> dict[str, Any]: ...

    def preview(self, request: CampaignPreviewRequest, *, require_flat: bool = False) -> dict[str, Any]: ...

    def execute(
        self,
        *,
        confirmation: str,
        campaign_id: str | None = None,
        event_sink: Callable[[Mapping[str, Any]], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]: ...

    def load(self, campaign_id: str) -> Any: ...

    def mark_interrupted_uncertain(self) -> list[str]: ...


WorkflowFactory = Callable[[TuiAccount, TuiAccountCatalog, CampaignRuntimePaths], CampaignWorkflow]
CatalogLoader = Callable[[Path], TuiAccountCatalog]

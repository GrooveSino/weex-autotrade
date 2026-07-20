from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import shlex
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from weex_cli.beta_allocation import BetaAllocation, HttpBetaAllocationProvider
from weex_cli.beta_volume import (
    MAX_BETA_DRIFT,
    BetaVolumePlan,
    BetaVolumePlanStore,
    LiveBetaVolumeService,
    inspect_live_account,
)
from weex_cli.errors import SafetyError, ValidationError
from weex_cli.gateway import WeexGateway
from weex_cli.live_profile import LiveProfile
from weex_cli.models import decimal_text, decimal_value
from weex_cli.reliability import NETWORK_ERRORS, ReadRetryPolicy, retry_read

DEFAULT_CAMPAIGN_DIRECTORY = Path("data/beta-volume-campaigns")
DEFAULT_CHILD_PLAN_DIRECTORY = Path("data/beta-volume-campaign-plans")
DEFAULT_AUTHORIZATION_MINUTES = 360
MAX_CAMPAIGN_RUNS = 20
MAX_HOLD_SECONDS = 3600.0
MAX_ROUND_GAP_SECONDS = 3600.0
DEFAULT_MAX_POSITION_QUOTE = "1200"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_RECOVERY_ATTEMPTS = 3
DEFAULT_MAX_EMPTY_ROUNDS = 3
CAMPAIGN_READ_RETRY_POLICY = ReadRetryPolicy(attempts=8, initial_delay_seconds=1, max_delay_seconds=8)
RETRYABLE_CHILD_REASONS = {"empty_round_limit_exhausted", "round_limit_exhausted"}
CAMPAIGN_CONFIRMATION_PATTERN = re.compile(
    r"EXECUTE WEEX LIVE BETA-CAMPAIGN (?P<campaign_id>WC-[0-9A-F]{10}) "
    r"RUNS_(?:[1-9]|1[0-9]|20) POST_ONLY"
)


@dataclass(frozen=True)
class BetaVolumeCampaign:
    schema_version: int
    campaign_id: str
    created_at_ms: int
    expires_at_ms: int
    profile_fingerprint: str
    target_turnover_quote: Decimal
    round_turnover_quote: Decimal
    max_position_quote: Decimal
    timeout_seconds: int
    recovery_attempts: int
    max_empty_rounds: int
    cooldown_seconds: float
    hold_min_seconds: float
    hold_max_seconds: float
    round_gap_min_seconds: float
    round_gap_max_seconds: float
    max_runs: int
    leverage: str | int
    max_auto_leverage: int
    margin_buffer: Decimal
    margin_mode: str
    allocation: BetaAllocation

    @classmethod
    def create(
        cls,
        gateway: WeexGateway,
        allocation: BetaAllocation,
        *,
        profile_fingerprint: str,
        target_turnover_quote: str | Decimal,
        round_turnover_quote: str | Decimal,
        max_position_quote: str | Decimal = DEFAULT_MAX_POSITION_QUOTE,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        recovery_attempts: int = DEFAULT_RECOVERY_ATTEMPTS,
        max_empty_rounds: int = DEFAULT_MAX_EMPTY_ROUNDS,
        hold_min_seconds: float = 0.0,
        hold_max_seconds: float = 0.0,
        round_gap_min_seconds: float = 1.0,
        round_gap_max_seconds: float = 1.0,
        max_runs: int = MAX_CAMPAIGN_RUNS,
        leverage: str | int = "auto",
        authorization_minutes: int = DEFAULT_AUTHORIZATION_MINUTES,
        now_ms: int | None = None,
    ) -> BetaVolumeCampaign:
        target = decimal_value(target_turnover_quote, name="target_turnover_quote")
        round_quote = decimal_value(round_turnover_quote, name="round_turnover_quote")
        max_position = decimal_value(max_position_quote, name="max_position_quote")
        assert target is not None and round_quote is not None and max_position is not None
        if not profile_fingerprint or len(profile_fingerprint) < 12:
            raise ValidationError("profile fingerprint is invalid")
        if not 1 <= max_runs <= MAX_CAMPAIGN_RUNS:
            raise ValidationError(f"max_runs must be between 1 and {MAX_CAMPAIGN_RUNS}")
        if not 1 <= authorization_minutes <= 1440:
            raise ValidationError("authorization_minutes must be between 1 and 1440")
        _validate_delay_range("hold", hold_min_seconds, hold_max_seconds, MAX_HOLD_SECONDS)
        _validate_delay_range("round_gap", round_gap_min_seconds, round_gap_max_seconds, MAX_ROUND_GAP_SECONDS)
        round_quote = min(round_quote, target)

        # Reuse the production sizing validator so campaign and child plans cannot drift apart.
        preview = BetaVolumePlan.create(
            gateway,
            allocation,
            target_turnover_quote=target,
            round_turnover_quote=round_quote,
            max_position_quote=max_position,
            timeout_seconds=timeout_seconds,
            recovery_attempts=recovery_attempts,
            max_empty_rounds=max_empty_rounds,
            cooldown_seconds=0.0,
            leverage=leverage,
            now_ms=now_ms,
        )
        created_at_ms = preview.created_at_ms
        campaign = cls(
            schema_version=2,
            campaign_id="",
            created_at_ms=created_at_ms,
            expires_at_ms=created_at_ms + authorization_minutes * 60_000,
            profile_fingerprint=profile_fingerprint,
            target_turnover_quote=target,
            round_turnover_quote=round_quote,
            max_position_quote=max_position,
            timeout_seconds=timeout_seconds,
            recovery_attempts=recovery_attempts,
            max_empty_rounds=max_empty_rounds,
            cooldown_seconds=0.0,
            hold_min_seconds=float(hold_min_seconds),
            hold_max_seconds=float(hold_max_seconds),
            round_gap_min_seconds=float(round_gap_min_seconds),
            round_gap_max_seconds=float(round_gap_max_seconds),
            max_runs=max_runs,
            leverage=preview.leverage,
            max_auto_leverage=preview.max_auto_leverage,
            margin_buffer=preview.margin_buffer,
            margin_mode=preview.margin_mode,
            allocation=allocation,
        )
        return campaign._with_computed_id()

    @property
    def authorized_max_turnover_quote(self) -> Decimal:
        return self.target_turnover_quote + self.round_turnover_quote

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "campaign_id": self.campaign_id,
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "mode": "live",
            "strategy": "btc_long_eth_short",
            "profile_fingerprint": self.profile_fingerprint,
            "target_turnover_quote": decimal_text(self.target_turnover_quote),
            "round_turnover_quote": decimal_text(self.round_turnover_quote),
            "authorized_max_turnover_quote": decimal_text(self.authorized_max_turnover_quote),
            "max_position_quote": decimal_text(self.max_position_quote),
            "timeout_seconds": self.timeout_seconds,
            "recovery_attempts": self.recovery_attempts,
            "max_empty_rounds": self.max_empty_rounds,
            "cooldown_seconds": self.cooldown_seconds,
            "hold_min_seconds": self.hold_min_seconds,
            "hold_max_seconds": self.hold_max_seconds,
            "round_gap_min_seconds": self.round_gap_min_seconds,
            "round_gap_max_seconds": self.round_gap_max_seconds,
            "max_runs": self.max_runs,
            "leverage": self.leverage,
            "max_auto_leverage": self.max_auto_leverage,
            "margin_buffer": decimal_text(self.margin_buffer),
            "margin_mode": self.margin_mode,
            "time_in_force": "POST_ONLY",
            "allocation": self.allocation.as_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BetaVolumeCampaign:
        allocation_row = payload.get("allocation")
        if not isinstance(allocation_row, Mapping):
            raise ValidationError("stored campaign allocation is invalid")
        try:
            allocation = BetaAllocation(
                beta=Decimal(str(allocation_row["beta"])),
                btc_long_weight=Decimal(str(allocation_row["btc_long_weight"])),
                eth_short_weight=Decimal(str(allocation_row["eth_short_weight"])),
                version=str(allocation_row["version"]),
                as_of_ms=int(allocation_row["as_of_ms"]),
                confidence=Decimal(str(allocation_row["confidence"])),
                confidence_threshold=Decimal(str(allocation_row["confidence_threshold"])),
                source=str(allocation_row["source"]),
                confidence_override=bool(allocation_row.get("confidence_override", False)),
            )
            schema_version = int(payload["schema_version"])
            cooldown_seconds = float(payload.get("cooldown_seconds", 0))
            campaign = cls(
                schema_version=schema_version,
                campaign_id=str(payload["campaign_id"]).lower(),
                created_at_ms=int(payload["created_at_ms"]),
                expires_at_ms=int(payload["expires_at_ms"]),
                profile_fingerprint=str(payload["profile_fingerprint"]),
                target_turnover_quote=Decimal(str(payload["target_turnover_quote"])),
                round_turnover_quote=Decimal(str(payload["round_turnover_quote"])),
                max_position_quote=Decimal(str(payload["max_position_quote"])),
                timeout_seconds=int(payload["timeout_seconds"]),
                recovery_attempts=int(payload["recovery_attempts"]),
                max_empty_rounds=int(payload["max_empty_rounds"]),
                cooldown_seconds=cooldown_seconds,
                hold_min_seconds=float(payload.get("hold_min_seconds", 0)),
                hold_max_seconds=float(payload.get("hold_max_seconds", 0)),
                round_gap_min_seconds=float(payload.get("round_gap_min_seconds", cooldown_seconds)),
                round_gap_max_seconds=float(payload.get("round_gap_max_seconds", cooldown_seconds)),
                max_runs=int(payload["max_runs"]),
                leverage=str(payload["leverage"]) if payload["leverage"] == "auto" else int(payload["leverage"]),
                max_auto_leverage=int(payload["max_auto_leverage"]),
                margin_buffer=Decimal(str(payload["margin_buffer"])),
                margin_mode=str(payload["margin_mode"]),
                allocation=allocation,
            )
        except (DecimalException, KeyError, TypeError, ValueError) as exc:
            raise ValidationError("stored campaign payload is invalid") from exc
        if (
            campaign.schema_version not in {1, 2}
            or not 1 <= campaign.max_runs <= MAX_CAMPAIGN_RUNS
            or campaign.expires_at_ms <= campaign.created_at_ms
            or campaign.expires_at_ms - campaign.created_at_ms > 86_400_000
            or campaign.target_turnover_quote <= 0
            or campaign.round_turnover_quote <= 0
            or campaign.round_turnover_quote > campaign.target_turnover_quote
            or campaign.max_position_quote <= 0
            or not _valid_delay_range(
                campaign.hold_min_seconds,
                campaign.hold_max_seconds,
                MAX_HOLD_SECONDS,
            )
            or not _valid_delay_range(
                campaign.round_gap_min_seconds,
                campaign.round_gap_max_seconds,
                MAX_ROUND_GAP_SECONDS,
            )
            or campaign.campaign_id != campaign._computed_id()
        ):
            raise ValidationError("stored campaign identity is invalid")
        return campaign

    def _with_computed_id(self) -> BetaVolumeCampaign:
        return BetaVolumeCampaign(**{**self.__dict__, "campaign_id": self._computed_id()})

    def _computed_id(self) -> str:
        fields = [
            str(self.schema_version),
            str(self.created_at_ms),
            str(self.expires_at_ms),
            self.profile_fingerprint,
            decimal_text(self.target_turnover_quote) or "0",
            decimal_text(self.round_turnover_quote) or "0",
            decimal_text(self.max_position_quote) or "0",
            str(self.timeout_seconds),
            str(self.recovery_attempts),
            str(self.max_empty_rounds),
            str(self.cooldown_seconds),
        ]
        if self.schema_version >= 2:
            fields.extend(
                (
                    str(self.hold_min_seconds),
                    str(self.hold_max_seconds),
                    str(self.round_gap_min_seconds),
                    str(self.round_gap_max_seconds),
                )
            )
        fields.extend(
            (
                str(self.max_runs),
                str(self.leverage),
                str(self.max_auto_leverage),
                decimal_text(self.margin_buffer) or "0",
                self.margin_mode,
                self.allocation.version,
                decimal_text(self.allocation.beta) or "0",
                decimal_text(self.allocation.btc_long_weight) or "0",
                decimal_text(self.allocation.eth_short_weight) or "0",
                str(self.allocation.as_of_ms),
            )
        )
        identity = "|".join(fields)
        return f"wc-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:10]}"


@dataclass(frozen=True)
class BetaVolumeCampaignRecord:
    campaign: BetaVolumeCampaign
    state: str
    result: Any = None


class BetaVolumeCampaignStore:
    def __init__(self, directory: Path = DEFAULT_CAMPAIGN_DIRECTORY) -> None:
        self.directory = directory

    def create(self, campaign: BetaVolumeCampaign) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{campaign.campaign_id}.json"
        payload = {
            "schema_version": campaign.schema_version,
            "state": "planned",
            "campaign": campaign.as_dict(),
            "result": None,
        }
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SafetyError(f"campaign already exists: {campaign.campaign_id}") from None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path

    def save(self, campaign: BetaVolumeCampaign, *, state: str, result: Any) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{campaign.campaign_id}.json"
        temporary = path.with_suffix(".tmp")
        payload = {
            "schema_version": campaign.schema_version,
            "state": state,
            "campaign": campaign.as_dict(),
            "result": result,
        }
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
        return path

    def claim_for_execution(self, campaign: BetaVolumeCampaign) -> None:
        record = self.load(campaign.campaign_id)
        if record.campaign != campaign or record.state != "planned":
            raise SafetyError("campaign is not in a pristine planned state")
        claim_path = self.directory / f"{campaign.campaign_id}.claim"
        try:
            descriptor = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SafetyError("campaign is already claimed or consumed") from None
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(str(int(time.time() * 1000)))
            handle.flush()
            os.fsync(handle.fileno())
        self.save(campaign, state="executing", result=None)

    def load(self, campaign_id: str) -> BetaVolumeCampaignRecord:
        normalized = campaign_id.lower()
        if not normalized.startswith("wc-") or not normalized[3:].isalnum():
            raise ValidationError("invalid campaign ID")
        path = self.directory / f"{normalized}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValidationError(f"campaign not found: {normalized}") from None
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            raise ValidationError(f"campaign is unreadable: {normalized}") from None
        if not isinstance(payload, Mapping):
            raise ValidationError("stored campaign schema is invalid")
        campaign_row = payload.get("campaign")
        if payload.get("schema_version") not in {1, 2} or not isinstance(campaign_row, Mapping):
            raise ValidationError("stored campaign schema is invalid")
        return BetaVolumeCampaignRecord(
            campaign=BetaVolumeCampaign.from_dict(campaign_row),
            state=str(payload.get("state") or "unknown"),
            result=payload.get("result"),
        )


ChildExecutor = Callable[[BetaVolumePlan], dict[str, Any]]
EventSink = Callable[[Mapping[str, Any]], None]


class LiveBetaVolumeCampaignService:
    def __init__(
        self,
        gateway: WeexGateway,
        provider: HttpBetaAllocationProvider,
        campaign_store: BetaVolumeCampaignStore,
        child_store: BetaVolumePlanStore,
        *,
        profile_fingerprint: str,
        child_executor: ChildExecutor | None = None,
        event_sink: EventSink | None = None,
        lane_gateways: Mapping[str, WeexGateway] | None = None,
        market_data: Any | None = None,
        order_updates: Any | None = None,
        stop_requested: Callable[[], bool] | None = None,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        sleep: Callable[[float], None] = time.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.gateway = gateway
        self.provider = provider
        self.campaign_store = campaign_store
        self.child_store = child_store
        self.profile_fingerprint = profile_fingerprint
        self.event_sink = event_sink
        self.lane_gateways = lane_gateways
        self.market_data = market_data
        self.order_updates = order_updates
        self.stop_requested = stop_requested or (lambda: False)
        self.now_ms = now_ms
        self.sleep = sleep
        self.uniform = uniform
        self.current_campaign: BetaVolumeCampaign | None = None
        self.child_executor = child_executor or self._execute_child

    def execute(self, campaign: BetaVolumeCampaign) -> dict[str, Any]:
        started_ms = self.now_ms()
        self.current_campaign = campaign
        self._validate_authorization(campaign)
        self._emit("campaign_boundary_started", phase="initial")
        initial_boundary = self._read_boundary()
        self._emit("campaign_boundary_completed", phase="initial")
        if not _boundary_is_flat(initial_boundary):
            raise SafetyError("campaign requires flat BTC/ETH positions and no regular or trigger orders")
        self.campaign_store.claim_for_execution(campaign)

        child_results: list[dict[str, Any]] = []
        total_quote = Decimal(0)
        for run_number in range(1, campaign.max_runs + 1):
            if self.stop_requested():
                return self._finish(campaign, "stopped", "stop_requested", total_quote, child_results, started_ms)
            if total_quote >= campaign.target_turnover_quote:
                break
            if self.now_ms() >= campaign.expires_at_ms:
                return self._finish(campaign, "stopped", "campaign_expired", total_quote, child_results, started_ms)

            remaining = campaign.target_turnover_quote - total_quote
            self._emit(
                "campaign_child_planning_started",
                campaign_id=campaign.campaign_id,
                run=run_number,
                remaining_quote=decimal_text(remaining),
            )
            try:
                child = self._read_with_retry(
                    lambda remaining=remaining, run_number=run_number: self._create_child(
                        campaign, remaining, run_number
                    ),
                    operation="child_planning",
                    run=run_number,
                )
            except NETWORK_ERRORS as exc:
                return self._finish(
                    campaign,
                    "stopped",
                    f"child_planning_network:{type(exc).__name__.lower()}",
                    total_quote,
                    child_results,
                    started_ms,
                )
            self._emit(
                "campaign_child_planning_completed",
                campaign_id=campaign.campaign_id,
                run=run_number,
                child_plan_id=child.plan_id,
            )
            self.child_store.create(child)
            self._emit(
                "campaign_run_started",
                campaign_id=campaign.campaign_id,
                run=run_number,
                child_plan_id=child.plan_id,
                remaining_quote=decimal_text(remaining),
            )
            try:
                child_result = self._execute_child_with_read_retry(child)
            except NETWORK_ERRORS as exc:
                child_state = self.child_store.load_record(child.plan_id).state
                status = "stopped" if child_state == "planned" else "uncertain"
                reason = f"child_{child_state}_network:{type(exc).__name__.lower()}"
                return self._finish(campaign, status, reason, total_quote, child_results, started_ms)
            except Exception as exc:  # noqa: BLE001 - campaign must checkpoint before returning control
                child_state = self.child_store.load_record(child.plan_id).state
                status = "stopped" if child_state == "planned" else "uncertain"
                reason = f"child_{child_state}_exception:{type(exc).__name__.lower()}"
                return self._finish(campaign, status, reason, total_quote, child_results, started_ms)

            child_results.append(child_result)
            try:
                child_quote = _authoritative_child_quote(child_result)
            except SafetyError:
                return self._finish(
                    campaign,
                    "stopped",
                    "child_accounting_not_verified_pure_maker",
                    total_quote,
                    child_results,
                    started_ms,
                )
            total_quote += child_quote
            if total_quote > campaign.authorized_max_turnover_quote:
                return self._finish(
                    campaign,
                    "uncertain",
                    "authorized_volume_ceiling_exceeded",
                    total_quote,
                    child_results,
                    started_ms,
                )

            self._emit("campaign_boundary_started", phase="checkpoint", run=run_number)
            try:
                boundary = self._read_boundary()
            except NETWORK_ERRORS:
                return self._finish(
                    campaign,
                    "uncertain",
                    "child_boundary_observation_unavailable",
                    total_quote,
                    child_results,
                    started_ms,
                    {"observation": "unavailable"},
                )
            self._emit("campaign_boundary_completed", phase="checkpoint", run=run_number)
            checkpoint = self._result(
                campaign,
                "executing",
                "child_checkpointed",
                total_quote,
                child_results,
                boundary,
                started_ms,
            )
            self.campaign_store.save(campaign, state="executing", result=checkpoint)
            self._emit(
                "campaign_run_completed",
                campaign_id=campaign.campaign_id,
                run=run_number,
                child_plan_id=child.plan_id,
                child_status=child_result.get("status"),
                child_quote=decimal_text(child_quote),
                total_quote=decimal_text(total_quote),
            )

            if not _boundary_is_flat(boundary):
                return self._finish(
                    campaign,
                    "uncertain",
                    "child_finished_without_confirmed_flat_boundary",
                    total_quote,
                    child_results,
                    started_ms,
                    boundary,
                )
            if child_result.get("status") == "uncertain":
                return self._finish(
                    campaign,
                    "uncertain",
                    str(child_result.get("reason") or "child_uncertain"),
                    total_quote,
                    child_results,
                    started_ms,
                    boundary,
                )
            if self.stop_requested():
                return self._finish(
                    campaign,
                    "stopped",
                    "stop_requested",
                    total_quote,
                    child_results,
                    started_ms,
                    boundary,
                )
            child_completed = child_result.get("status") == "completed"
            if not child_completed and child_result.get("reason") not in RETRYABLE_CHILD_REASONS:
                return self._finish(
                    campaign,
                    "stopped",
                    str(child_result.get("reason") or "child_stopped"),
                    total_quote,
                    child_results,
                    started_ms,
                    boundary,
                )
            if total_quote >= campaign.target_turnover_quote:
                if not child_completed:
                    return self._finish(
                        campaign,
                        "stopped",
                        "target_reached_by_noncompleted_child",
                        total_quote,
                        child_results,
                        started_ms,
                        boundary,
                    )
                return self._finish(
                    campaign,
                    "completed",
                    "campaign_target_completed",
                    total_quote,
                    child_results,
                    started_ms,
                    boundary,
                )
            if child_completed:
                return self._finish(
                    campaign,
                    "stopped",
                    "child_completed_below_campaign_target",
                    total_quote,
                    child_results,
                    started_ms,
                    boundary,
                )

        return self._finish(
            campaign,
            "stopped",
            "campaign_run_limit_exhausted",
            total_quote,
            child_results,
            started_ms,
        )

    def _validate_authorization(self, campaign: BetaVolumeCampaign) -> None:
        if campaign.schema_version not in {1, 2}:
            raise SafetyError("unsupported campaign schema")
        if campaign.profile_fingerprint != self.profile_fingerprint:
            raise SafetyError("campaign was authorized for a different live profile")
        if self.now_ms() >= campaign.expires_at_ms:
            raise SafetyError("campaign authorization expired; create a new dry run")
        current = self._read_with_retry(self.provider.get, operation="beta_allocation")
        drift = abs(current.beta - campaign.allocation.beta) / campaign.allocation.beta
        if drift > MAX_BETA_DRIFT:
            raise SafetyError("Beta moved more than 5% since campaign planning")

    def _create_child(self, campaign: BetaVolumeCampaign, target: Decimal, run_number: int) -> BetaVolumePlan:
        created_at_ms = self.now_ms() + run_number
        try:
            return BetaVolumePlan.create(
                self.gateway,
                campaign.allocation,
                target_turnover_quote=target,
                round_turnover_quote=min(campaign.round_turnover_quote, target),
                max_position_quote=campaign.max_position_quote,
                timeout_seconds=campaign.timeout_seconds,
                recovery_attempts=campaign.recovery_attempts,
                max_empty_rounds=campaign.max_empty_rounds,
                cooldown_seconds=campaign.cooldown_seconds,
                leverage=campaign.leverage,
                max_auto_leverage=campaign.max_auto_leverage,
                margin_buffer=campaign.margin_buffer,
                margin_mode=campaign.margin_mode,
                now_ms=created_at_ms,
            )
        except ValidationError as exc:
            if "below the current" not in str(exc):
                raise
            fallback_target = min(campaign.round_turnover_quote, campaign.authorized_max_turnover_quote - target)
            if fallback_target <= 0:
                raise
            return BetaVolumePlan.create(
                self.gateway,
                campaign.allocation,
                target_turnover_quote=fallback_target,
                round_turnover_quote=fallback_target,
                max_position_quote=campaign.max_position_quote,
                timeout_seconds=campaign.timeout_seconds,
                recovery_attempts=campaign.recovery_attempts,
                max_empty_rounds=campaign.max_empty_rounds,
                cooldown_seconds=campaign.cooldown_seconds,
                leverage=campaign.leverage,
                max_auto_leverage=campaign.max_auto_leverage,
                margin_buffer=campaign.margin_buffer,
                margin_mode=campaign.margin_mode,
                now_ms=created_at_ms,
            )

    def _execute_child_with_read_retry(self, child: BetaVolumePlan) -> dict[str, Any]:
        for attempt in range(1, CAMPAIGN_READ_RETRY_POLICY.attempts + 1):
            try:
                return self.child_executor(child)
            except NETWORK_ERRORS:
                state = self.child_store.load_record(child.plan_id).state
                if state != "planned" or attempt >= CAMPAIGN_READ_RETRY_POLICY.attempts:
                    raise
                delay = CAMPAIGN_READ_RETRY_POLICY.delay_after(attempt)
                self._emit(
                    "campaign_read_retry",
                    child_plan_id=child.plan_id,
                    operation="child_preflight",
                    attempt=attempt + 1,
                    max_attempts=CAMPAIGN_READ_RETRY_POLICY.attempts,
                    seconds=delay,
                )
                self.sleep(delay)
        raise AssertionError("unreachable")

    def _read_with_retry(
        self,
        reader: Callable[[], Any],
        *,
        operation: str,
        **fields: Any,
    ) -> Any:
        def on_retry(event: Mapping[str, object]) -> None:
            self._emit(
                "campaign_read_retry",
                operation=operation,
                attempt=event.get("next_attempt"),
                max_attempts=event.get("max_attempts"),
                seconds=event.get("delay_seconds"),
                error=event.get("error"),
                **fields,
            )

        return retry_read(
            reader,
            operation=operation,
            policy=CAMPAIGN_READ_RETRY_POLICY,
            sleep=self.sleep,
            retry_sink=on_retry,
        )

    def _execute_child(self, child: BetaVolumePlan) -> dict[str, Any]:
        campaign = self.current_campaign
        if campaign is None:
            raise SafetyError("campaign timing policy is unavailable")
        service = LiveBetaVolumeService(
            self.gateway,
            self.provider,
            self.child_store,
            event_sink=self.event_sink,
            lane_gateways=self.lane_gateways,
            market_data=self.market_data,
            order_updates=self.order_updates,
            now_ms=self.now_ms,
            sleep=self.sleep,
            hold_delay_seconds=lambda round_number: self._sample_delay(
                campaign.hold_min_seconds,
                campaign.hold_max_seconds,
            ),
            round_gap_delay_seconds=lambda round_number: self._sample_delay(
                campaign.round_gap_min_seconds,
                campaign.round_gap_max_seconds,
            ),
        )
        return service.execute(child)

    def _sample_delay(self, minimum: float, maximum: float) -> float:
        if minimum == maximum:
            return minimum
        return self.uniform(minimum, maximum)

    def _read_boundary(self) -> dict[str, Any]:
        return self._read_with_retry(
            lambda: inspect_live_account(self.gateway, Decimal(0)),
            operation="account_boundary",
        )

    def _finish(
        self,
        campaign: BetaVolumeCampaign,
        status: str,
        reason: str,
        total_quote: Decimal,
        child_results: list[dict[str, Any]],
        started_ms: int,
        boundary: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if boundary is None:
            try:
                boundary = self._read_boundary()
            except NETWORK_ERRORS:
                boundary = {"observation": "unavailable"}
                status = "uncertain"
                reason = "final_boundary_observation_unavailable"
        result = self._result(campaign, status, reason, total_quote, child_results, boundary, started_ms)
        self.campaign_store.save(campaign, state=status, result=result)
        self._emit(
            "campaign_finished",
            campaign_id=campaign.campaign_id,
            status=status,
            reason=reason,
            total_quote=decimal_text(total_quote),
        )
        return result

    def _result(
        self,
        campaign: BetaVolumeCampaign,
        status: str,
        reason: str,
        total_quote: Decimal,
        child_results: list[dict[str, Any]],
        boundary: Mapping[str, Any],
        started_ms: int,
    ) -> dict[str, Any]:
        positive_children: list[dict[str, Any]] = []
        accounting_parse_failed = False
        for row in child_results:
            try:
                if _child_quote(row) > 0:
                    positive_children.append(row)
            except SafetyError:
                accounting_parse_failed = True
        return {
            "schema_version": 1,
            "kind": "beta_volume_campaign_execution",
            "mode": "live",
            "status": status,
            "reason": reason,
            "campaign_id": campaign.campaign_id,
            "target_turnover_quote": decimal_text(campaign.target_turnover_quote),
            "executed_quote_volume": decimal_text(total_quote),
            "remaining_quote": decimal_text(max(Decimal(0), campaign.target_turnover_quote - total_quote)),
            "excess_quote": decimal_text(max(Decimal(0), total_quote - campaign.target_turnover_quote)),
            "authorized_max_turnover_quote": decimal_text(campaign.authorized_max_turnover_quote),
            "maker_only": (
                not accounting_parse_failed
                and bool(positive_children)
                and all(_child_is_authoritative(row) for row in positive_children)
            ),
            "runs_used": len(child_results),
            "max_runs": campaign.max_runs,
            "elapsed_ms": self.now_ms() - started_ms,
            "final_boundary": dict(boundary),
            "children": child_results,
            "retry_allowed": False,
        }

    def _emit(self, event: str, **payload: Any) -> None:
        if self.event_sink is not None:
            self.event_sink({"event": event, **payload})


def campaign_confirmation(campaign: BetaVolumeCampaign) -> str:
    return f"EXECUTE WEEX LIVE BETA-CAMPAIGN {campaign.campaign_id.upper()} RUNS_{campaign.max_runs} POST_ONLY"


def campaign_id_from_confirmation(confirmation: str) -> str:
    match = CAMPAIGN_CONFIRMATION_PATTERN.fullmatch(confirmation)
    if match is None:
        raise ValidationError("invalid Beta campaign confirmation phrase")
    return match.group("campaign_id").lower()


def live_profile_fingerprint(profile: LiveProfile) -> str:
    proxy = urlsplit(profile.proxy_url or "")
    api_key_digest = hashlib.sha256(profile.settings.credentials.api_key.encode("utf-8")).hexdigest()
    identity = "|".join(
        (
            str(profile.path.resolve()),
            api_key_digest,
            proxy.scheme,
            proxy.hostname or "",
            str(proxy.port or ""),
            str(profile.allow_live_mutations),
            str(profile.post_only_only),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def campaign_plan_payload(
    campaign: BetaVolumeCampaign,
    path: Path,
    account_readiness: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "beta_volume_campaign_plan",
        "status": "dry_run",
        "campaign": campaign.as_dict(),
        "account_readiness": dict(account_readiness),
        "plan_path": str(path),
        "confirm": campaign_confirmation(campaign),
        "timing": {
            "hold_seconds": [campaign.hold_min_seconds, campaign.hold_max_seconds],
            "round_gap_seconds": [campaign.round_gap_min_seconds, campaign.round_gap_max_seconds],
            "selection": "uniform_per_cycle",
        },
        "safety": {
            "single_bounded_authorization": True,
            "post_only": True,
            "authoritative_user_trades_ledger": True,
            "continue_only_from_confirmed_flat_boundary": True,
            "no_automatic_submit_retry_after_uncertainty": True,
            "hard_run_limit": campaign.max_runs,
        },
    }


def campaign_execute_command(campaign: BetaVolumeCampaign, profile_path: Path) -> str:
    phrase = shlex.quote(campaign_confirmation(campaign))
    profile = shlex.quote(str(profile_path))
    return f"WEEX_LIVE_TRADING_ENABLED=true ./weex --profile {profile} live beta-campaign --execute --confirm {phrase}"


def _boundary_is_flat(boundary: Mapping[str, Any]) -> bool:
    keys = ("active_position_count", "regular_order_count", "trigger_order_count")
    try:
        return all(key in boundary and int(boundary[key]) == 0 for key in keys)
    except (TypeError, ValueError):
        return False


def _child_quote(result: Mapping[str, Any]) -> Decimal:
    try:
        quote = Decimal(str(result.get("executed_quote_volume") or 0))
    except (DecimalException, TypeError, ValueError):
        raise SafetyError("child reported invalid quote volume") from None
    if not quote.is_finite() or quote < 0:
        raise SafetyError("child reported invalid quote volume")
    return quote


def _child_is_authoritative(result: Mapping[str, Any]) -> bool:
    accounting = result.get("accounting")
    if not isinstance(accounting, Mapping):
        return False
    return (
        bool(accounting.get("verified"))
        and bool(accounting.get("maker_only"))
        and int(accounting.get("taker_count") or 0) == 0
        and int(accounting.get("unknown_liquidity_count") or 0) == 0
    )


def _authoritative_child_quote(result: Mapping[str, Any]) -> Decimal:
    quote = _child_quote(result)
    if quote == 0:
        return quote
    if not _child_is_authoritative(result):
        raise SafetyError("child volume is not verified pure Maker userTrades volume")
    return quote


def _validate_delay_range(name: str, minimum: float, maximum: float, ceiling: float) -> None:
    if not _valid_delay_range(minimum, maximum, ceiling):
        raise ValidationError(f"{name} range must be finite, non-negative, ordered, and at most {ceiling:g} seconds")


def _valid_delay_range(minimum: float, maximum: float, ceiling: float) -> bool:
    return math.isfinite(minimum) and math.isfinite(maximum) and 0 <= minimum <= maximum <= ceiling

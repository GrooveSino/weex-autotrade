from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from pydantic import SecretStr

from .campaign_log import campaign_event_log
from .execution import CycleExecutionStatus, ExecutionRecord, PositionCloseExecutionResult
from .funding import funding_preflight
from .models import (
    AccountInstance,
    CreateInstanceRequest,
    CycleSnapshot,
    ExposureSnapshot,
    FundingPreflightStatus,
    InstanceAction,
    InstanceStatus,
    LogBatch,
    LogLevel,
    LogLine,
    ProxySnapshot,
    ProxyStatus,
    ProxyType,
    RuntimeHealthSnapshot,
    StrategyProgress,
    StrategyStage,
    StrategyTargetMode,
    UpdateInstanceRequest,
    VolumeSnapshot,
    VolumeStrategy,
    VolumeStrategyInput,
    WalletSnapshot,
    default_volume_strategy,
)
from .ownership import LEGACY_OWNER_USER_ID, current_owner_user_id
from .proxy import ProxyValidationError, normalize_proxy_url, proxy_host
from .repository import AccountRepository
from .strategy import estimate_rounds, target_progress_quote
from .telemetry import AccountTelemetry
from .vault import CredentialMaterial, CredentialVault
from .volume_history import TradeVolumeAggregate


class FleetError(RuntimeError):
    status_code = 400


class InstanceNotFound(FleetError):
    status_code = 404


class StrategyNotFound(FleetError):
    status_code = 404


class UnsafeOperation(FleetError):
    status_code = 409


class ValidationFailed(FleetError):
    status_code = 422


class TelemetryUnavailable(FleetError):
    status_code = 503


class BetaSourceUnavailable(TelemetryUnavailable):
    """The Final Beta source cannot safely produce a new Live preview."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class FleetControlService:
    def __init__(
        self,
        repository: AccountRepository,
        vault: CredentialVault,
        *,
        adapter: str = "mock",
        mock_cycle_total_quote: Decimal = Decimal("20"),
    ) -> None:
        if adapter not in {"mock", "weex-readonly", "weex-live"}:
            raise ValueError("unsupported control-plane adapter")
        if not mock_cycle_total_quote.is_finite() or mock_cycle_total_quote <= 0:
            raise ValueError("mock cycle total quote must be finite and positive")
        self.repository = repository
        self.vault = vault
        self.adapter = adapter
        self.mock_cycle_total_quote = mock_cycle_total_quote
        self._reconcile_strategy_catalog()

    def list_instances(self) -> list[AccountInstance]:
        return self.repository.list()

    def get_instance(self, instance_id: str) -> AccountInstance:
        instance = self.repository.get(instance_id)
        if instance is None:
            raise InstanceNotFound(f"instance {instance_id!r} was not found")
        return instance

    def list_strategies(self) -> list[VolumeStrategy]:
        return self.repository.list_strategies()

    def get_strategy(self, strategy_id: str) -> VolumeStrategy:
        strategy = self.repository.get_strategy(strategy_id)
        if strategy is None:
            raise StrategyNotFound(f"strategy {strategy_id!r} was not found")
        return strategy

    def create_strategy(self, request: VolumeStrategyInput) -> VolumeStrategy:
        strategy = VolumeStrategy(
            id=f"strategy-{uuid4().hex[:10]}",
            owner_user_id=current_owner_user_id() or LEGACY_OWNER_USER_ID,
            version=1,
            **request.model_dump(),
        )
        return self.repository.create_strategy(strategy)

    def update_strategy(self, strategy_id: str, request: VolumeStrategyInput) -> VolumeStrategy:
        current = self.get_strategy(strategy_id)
        updated_strategy = VolumeStrategy(
            id=current.id,
            owner_user_id=current.owner_user_id,
            version=current.version + 1,
            **request.model_dump(),
        )
        target_mode_changed = updated_strategy.target_mode is not current.target_mode
        assigned = [instance for instance in self.repository.list() if instance.strategy_id == strategy_id]
        for instance in assigned:
            self._ensure_configurable(instance, "changing a shared strategy")
            projected_progress = (
                Decimal(0) if target_mode_changed else target_progress_quote(instance, updated_strategy)
            )
            if request.target_volume_quote < projected_progress:
                raise ValidationFailed(
                    f"strategy target cannot be lower than current target progress for instance {instance.id!r}"
                )

        projected = [
            self._strategy_projection(instance, updated_strategy, reset_progress=target_mode_changed)
            for instance in assigned
        ]
        self.repository.replace_strategy_and_instances(updated_strategy, projected)
        for instance in projected:
            self._append_log(instance.id, LogLevel.INFO, f"共享策略已更新：{updated_strategy.name}")
        return updated_strategy

    def delete_strategy(self, strategy_id: str) -> None:
        self.get_strategy(strategy_id)
        assigned = [instance.id for instance in self.repository.list() if instance.strategy_id == strategy_id]
        if assigned:
            raise UnsafeOperation(f"strategy is assigned to {len(assigned)} instance(s)")
        self.repository.delete_strategy(strategy_id)

    def assign_strategy(self, strategy_id: str, instance_ids: list[str]) -> list[AccountInstance]:
        strategy = self.get_strategy(strategy_id)
        instances = [self.get_instance(instance_id) for instance_id in instance_ids]
        for instance in instances:
            self._ensure_configurable(instance, "assigning a strategy")
        updated = [self._strategy_projection(instance, strategy, reset_progress=True) for instance in instances]
        self.repository.replace_many(updated)
        for instance in updated:
            self._append_log(instance.id, LogLevel.INFO, f"已绑定共享策略：{strategy.name}；策略进度已重置")
        return updated

    def create_instance(self, request: CreateInstanceRequest) -> AccountInstance:
        host = "不使用代理"
        normalized_proxy: str | None = None
        if request.proxy.type is not ProxyType.NONE:
            assert request.proxy.url is not None
            try:
                host = proxy_host(request.proxy.type, request.proxy.url.get_secret_value())
                normalized_proxy = normalize_proxy_url(request.proxy.type, request.proxy.url.get_secret_value())
            except ProxyValidationError as exc:
                raise ValidationFailed(str(exc)) from exc

        instance_id = f"ins-{uuid4().hex[:10]}"
        api_key = request.credentials.api_key.get_secret_value()
        legacy_total_quote = request.mock_cycle_total_quote or self.mock_cycle_total_quote
        if request.strategy_id is not None:
            strategy = self.get_strategy(request.strategy_id)
        elif {"cycle_target", "mock_cycle_total_quote"} & request.model_fields_set:
            strategy = self._create_legacy_strategy(
                cycle_target=request.cycle_target,
                legacy_total_quote=legacy_total_quote,
            )
        else:
            strategy = self._default_strategy()
        estimated_rounds = estimate_rounds(strategy).maximum
        initial_wallet = (
            WalletSnapshot(equity=1000, available=820)
            if self.adapter == "mock" and request.mode.value == "demo"
            else WalletSnapshot()
        )
        instance = AccountInstance(
            id=instance_id,
            owner_user_id=current_owner_user_id() or LEGACY_OWNER_USER_ID,
            name=request.name.strip(),
            account_tag=request.account_tag.strip() or "未分组",
            api_key_tail=api_key[-4:].upper().rjust(4, "-"),
            mode=request.mode,
            status=InstanceStatus.STOPPED,
            phase="等待连接验证",
            proxy=ProxySnapshot(type=request.proxy.type, host=host),
            wallet=initial_wallet,
            cycle=CycleSnapshot(target=max(1, estimated_rounds)),
            strategy_id=strategy.id,
            strategy=strategy,
            mock_cycle_total_quote=(legacy_total_quote if self.adapter == "mock" else None),
            history_start_at_ms=request.history_start_at_ms,
        )
        instance = self._with_funding(instance)
        material = CredentialMaterial(
            api_key=request.credentials.api_key,
            api_secret=request.credentials.api_secret,
            passphrase=request.credentials.passphrase,
            proxy_url=SecretStr(normalized_proxy) if normalized_proxy is not None else None,
        )
        created = self.repository.create(instance)
        try:
            self.vault.put(instance_id, material)
        except Exception:
            self.repository.delete(instance_id)
            raise
        creation_message = (
            "实例已创建；当前使用模拟交易适配器"
            if self.adapter == "mock"
            else "实例已创建；Beta Campaign 实盘执行保持独立，普通策略入口不会下单"
            if self.adapter == "weex-live"
            else "实例已创建；WEEX 适配器为只读模式，已禁用实盘执行"
        )
        self._append_log(instance_id, LogLevel.INFO, creation_message)
        return created

    def apply_action(self, instance_id: str, action: InstanceAction) -> AccountInstance:
        instance = self.get_instance(instance_id)
        funding = self._with_funding(instance).funding_preflight
        progress = instance.strategy_progress
        if action is InstanceAction.START:
            if self.adapter != "mock":
                raise UnsafeOperation("the WEEX read-only adapter cannot start ordinary trading instances")
            if instance.mode.value == "live":
                raise UnsafeOperation("Live instances cannot start while the mock adapter is active")
            if instance.status is InstanceStatus.ERROR:
                raise UnsafeOperation("resolve the instance error before starting")
            if instance.status is InstanceStatus.WARNING:
                raise UnsafeOperation("refresh and verify telemetry before starting")
            if progress.system_pause_reason is not None:
                raise UnsafeOperation("the system pause must be resolved before starting")
            opening_new_pair = instance.strategy_progress.stage is not StrategyStage.HOLDING
            if (
                opening_new_pair
                and instance.strategy.target_mode is StrategyTargetMode.INCREMENTAL
                and (
                    progress.started_at_ms is not None
                    or progress.generated_volume_quote > 0
                    or progress.stage is StrategyStage.COMPLETE
                )
            ):
                # An incremental target describes one run, not the account's
                # permanent target. A new explicit start begins a fresh run.
                progress = StrategyProgress()
            target_progress = (
                Decimal(str(instance.volume.lifetime))
                if instance.strategy.target_mode is StrategyTargetMode.LIFETIME
                else progress.generated_volume_quote
            )
            if opening_new_pair and target_progress >= instance.strategy.target_volume_quote:
                raise UnsafeOperation("volume target has already been reached")
            if (
                opening_new_pair
                and instance.strategy.target_mode is StrategyTargetMode.LIFETIME
                and not instance.volume.complete
            ):
                raise UnsafeOperation("complete lifetime trade history synchronization before starting")
            if opening_new_pair and funding.status is not FundingPreflightStatus.READY:
                raise UnsafeOperation(f"funding preflight failed: {funding.reason}")
            if (
                instance.strategy.target_mode is StrategyTargetMode.INCREMENTAL
                and progress.started_at_ms is None
                and progress.generated_volume_quote == 0
            ):
                progress = progress.model_copy(update={"started_at_ms": time.time_ns() // 1_000_000})
            status = InstanceStatus.RUNNING
            if instance.strategy_progress.stage is StrategyStage.HOLDING:
                phase = "Mock 已恢复；等待到时平仓"
                next_action = "等待平仓"
            else:
                phase = "Mock 成交量策略运行中"
                next_action = "等待规划"
        elif action is InstanceAction.PAUSE:
            if instance.status is not InstanceStatus.RUNNING:
                raise UnsafeOperation("only a running instance can be paused")
            status = InstanceStatus.PAUSED
            phase = "已人工暂停"
            next_action = None
            progress = progress.model_copy(update={"system_pause_reason": None})
        else:
            status = InstanceStatus.STOPPED
            phase = "等待启动"
            next_action = None
            progress = progress.model_copy(update={"system_pause_reason": None})

        updated = instance.model_copy(
            update={
                "status": status,
                "phase": phase,
                "cycle": instance.cycle.model_copy(update={"next_action_at": next_action}),
                "strategy_progress": progress,
                "funding_preflight": funding,
                "runtime": instance.runtime.model_copy(update={"last_stop_verified_at_ms": None})
                if action is InstanceAction.START
                else instance.runtime,
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        action_label = {InstanceAction.START: "启动", InstanceAction.PAUSE: "暂停", InstanceAction.STOP: "停止"}[action]
        self._append_log(instance_id, LogLevel.INFO, f"实例操作已接受：{action_label}")
        return updated

    def project_bound_strategy_execution(
        self,
        instance_id: str,
        campaign_status: str,
        reason: str | None = None,
    ) -> AccountInstance:
        """Project executor-owned Live state without reusing the Mock action state machine."""
        instance = self.get_instance(instance_id)
        if instance.mode.value != "live":
            return instance
        status_map = {
            "executing": InstanceStatus.RUNNING,
            "stopping": InstanceStatus.PAUSED,
            "completed": InstanceStatus.STOPPED,
            "stopped": InstanceStatus.STOPPED,
            "uncertain": InstanceStatus.WARNING,
        }
        projected = status_map.get(campaign_status)
        if projected is None:
            return instance
        phase_map = {
            "executing": "已绑定策略实盘执行中",
            "stopping": "已绑定策略安全停止中；等待撤单与成交核验",
            "completed": "已绑定策略本次授权完成；请核验成交账本",
            "stopped": "已绑定策略已安全停止",
            "uncertain": "已绑定策略结果待人工对账",
        }
        phase = phase_map[campaign_status]
        if reason:
            phase = f"{phase} ({reason[:64]})"
        if instance.status is projected and instance.phase == phase:
            return instance
        updated = instance.model_copy(
            update={
                "status": projected,
                "phase": phase,
                "cycle": instance.cycle.model_copy(update={"next_action_at": None}),
                "updated_at": "刚刚",
            },
            deep=True,
        )
        self.repository.replace(updated)
        return updated

    def pause_for_beta(self, instance_id: str, reason_code: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        if instance.status is not InstanceStatus.RUNNING:
            return instance
        system_pause_reason = f"beta:{reason_code}"[:96]
        progress = instance.strategy_progress.model_copy(update={"system_pause_reason": system_pause_reason})
        updated = instance.model_copy(
            update={
                "status": InstanceStatus.PAUSED,
                "phase": f"Beta 服务异常，系统已暂停 ({reason_code})",
                "cycle": instance.cycle.model_copy(update={"next_action_at": None}),
                "strategy_progress": progress,
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(
            instance_id,
            LogLevel.ERROR,
            f"Beta 服务异常：{reason_code}；策略已暂停，开始撤销活动挂单",
        )
        return updated

    def resume_beta_pause(self, instance_id: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        reason = instance.strategy_progress.system_pause_reason
        if instance.status is not InstanceStatus.PAUSED or reason is None or not reason.startswith("beta:"):
            return instance
        holding = instance.strategy_progress.stage is StrategyStage.HOLDING
        progress = instance.strategy_progress.model_copy(update={"system_pause_reason": None})
        updated = instance.model_copy(
            update={
                "status": InstanceStatus.RUNNING,
                "phase": "Beta 服务已恢复；继续等待平仓" if holding else "Beta 服务已恢复；策略继续运行",
                "cycle": instance.cycle.model_copy(update={"next_action_at": "等待平仓" if holding else "等待规划"}),
                "strategy_progress": progress,
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(instance_id, LogLevel.SUCCESS, "Beta 服务已恢复；系统暂停已自动解除")
        return updated

    def pause_for_position_mismatch(self, instance_id: str, reason_code: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        if instance.status is InstanceStatus.ERROR:
            return instance
        progress = instance.strategy_progress.model_copy(update={"system_pause_reason": f"position:{reason_code}"[:96]})
        updated = instance.model_copy(
            update={
                "status": InstanceStatus.PAUSED,
                "phase": f"仓位与周期不一致，系统已暂停 ({reason_code})",
                "cycle": instance.cycle.model_copy(update={"next_action_at": None}),
                "strategy_progress": progress,
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(
            instance_id,
            LogLevel.WARN,
            f"检测到仓位异常：{reason_code}；已暂停且不会自动补腿",
        )
        return updated

    def record_manual_pair_close(self, instance_id: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        if instance.strategy_progress.stage is StrategyStage.COMPLETE:
            phase = "已核对人工双腿平仓；目标交易量已完成"
        elif instance.status is InstanceStatus.RUNNING:
            phase = "已核对人工双腿平仓；等待下一轮"
        elif instance.status is InstanceStatus.PAUSED:
            phase = "已核对人工双腿平仓；实例仍保持暂停"
        else:
            phase = "已核对人工双腿平仓；实例保持停止"
        updated = instance.model_copy(
            update={
                "phase": phase,
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(
            instance_id,
            LogLevel.WARN,
            "检测到 BTC 与 ETH 已被人工全部平仓；已按真实成交历史继续核对进度",
        )
        return updated

    def record_positions_closed(
        self,
        instance_id: str,
        result: PositionCloseExecutionResult,
        aggregate: TradeVolumeAggregate,
        *,
        strategy_generated_volume_quote: Decimal | None,
    ) -> AccountInstance:
        if result.outcome.status is not CycleExecutionStatus.COMPLETED:
            raise ValueError("only a completed position close can update the account projection")
        instance = self.get_instance(instance_id)
        progress = instance.strategy_progress
        if (
            instance.strategy.target_mode is StrategyTargetMode.INCREMENTAL
            and strategy_generated_volume_quote is not None
        ):
            progress = progress.model_copy(
                update={"generated_volume_quote": max(Decimal(0), strategy_generated_volume_quote)}
            )

        record = result.record
        if record is not None:
            if record.status is not CycleExecutionStatus.COMPLETED or progress.active_cycle_id != record.plan.cycle_id:
                raise UnsafeOperation("completed close does not match the active pair cycle")
            completed_cycles = record.plan.sequence
        else:
            if progress.stage is StrategyStage.HOLDING or progress.active_cycle_id is not None:
                raise UnsafeOperation("snapshot close cannot replace an unmatched active pair cycle")
            completed_cycles = instance.cycle.completed

        volume = VolumeSnapshot(
            lifetime=float(aggregate.lifetime),
            today=float(aggregate.today),
            complete=aggregate.complete,
        )
        projected = instance.model_copy(
            update={"volume": volume, "strategy_progress": progress},
            deep=True,
        )
        target_reached = target_progress_quote(projected) >= instance.strategy.target_volume_quote
        progress = progress.model_copy(
            update={
                "stage": StrategyStage.COMPLETE
                if target_reached
                else (StrategyStage.COOLDOWN if record is not None else StrategyStage.IDLE),
                "next_action_at_ms": None,
                "active_cycle_id": None,
            }
        )
        if target_reached:
            phase = "一键平仓完成；目标交易量已完成"
        elif instance.status is InstanceStatus.PAUSED:
            phase = "一键平仓完成；策略保持暂停"
        elif instance.status in {InstanceStatus.WARNING, InstanceStatus.ERROR}:
            phase = "一键平仓完成；原状态保留待处理"
        else:
            phase = "一键平仓完成；策略保持停止"
        status = (
            InstanceStatus.STOPPED
            if target_reached and instance.status in {InstanceStatus.STOPPED, InstanceStatus.PAUSED}
            else instance.status
        )
        updated = instance.model_copy(
            update={
                "status": status,
                "phase": phase,
                "volume": volume,
                "exposure": ExposureSnapshot(),
                "cycle": instance.cycle.model_copy(update={"completed": completed_cycles, "next_action_at": None}),
                "strategy_progress": progress,
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        updated = self._with_funding(updated)
        self.repository.replace(updated)
        amounts = {leg.symbol: leg.fill.quote_volume for leg in result.outcome.legs}
        self._append_log(
            instance_id,
            LogLevel.SUCCESS,
            f"一键平仓完成：BTC {amounts.get('BTCUSDT', Decimal(0))} + "
            f"ETH {amounts.get('ETHUSDT', Decimal(0))} USDT；"
            f"确认新增成交量 {result.closed_quote} USDT；策略未恢复",
        )
        return updated

    def record_order_cancel_verified(
        self,
        instance_id: str,
        *,
        canceled_count: int,
        reason: str,
        marks_stop_verified: bool = False,
    ) -> AccountInstance:
        instance = self.get_instance(instance_id)
        verified_at_ms = time.time_ns() // 1_000_000
        updated = instance.model_copy(
            update={
                "runtime": instance.runtime.model_copy(
                    update={"last_stop_verified_at_ms": verified_at_ms}
                    if marks_stop_verified
                    else {}
                ),
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(
            instance_id,
            LogLevel.SUCCESS,
            f"撤单核验完成：撤销 {canceled_count} 个活动挂单；结果 {reason}",
        )
        return updated

    def record_order_cancel_failure(self, instance_id: str, *, origin: str, reason: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        system_pause_reason = instance.strategy_progress.system_pause_reason or f"cancel_unverified:{origin}"
        progress = instance.strategy_progress.model_copy(update={"system_pause_reason": system_pause_reason[:96]})
        updated = instance.model_copy(
            update={
                "status": InstanceStatus.ERROR,
                "phase": f"撤单状态未核验，禁止继续运行 ({reason})",
                "cycle": instance.cycle.model_copy(update={"next_action_at": None}),
                "strategy_progress": progress,
                "runtime": instance.runtime.model_copy(
                    update={
                        "consecutive_failures": instance.runtime.consecutive_failures + 1,
                        "last_error_type": "OrderCancellationUnverified",
                        "last_stop_verified_at_ms": None,
                    }
                ),
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(
            instance_id,
            LogLevel.ERROR,
            f"撤单核验失败：{reason}；实例保持停止推进，必须重新核对",
        )
        return updated

    def record_global_stop(self, instance_id: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        updated = instance.model_copy(
            update={
                "phase": "全局停止已触发",
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(instance_id, LogLevel.WARN, "全局停止已完成，活动挂单撤销状态已核验")
        return updated

    def update_instance(self, instance_id: str, request: UpdateInstanceRequest) -> AccountInstance:
        instance = self.get_instance(instance_id)
        if instance.status not in {InstanceStatus.STOPPED, InstanceStatus.ERROR}:
            raise UnsafeOperation("stop the instance before changing account configuration")
        if instance.status is InstanceStatus.ERROR:
            recovery_fields = {"name", "account_tag", "credentials", "proxy"}
            if not request.model_fields_set <= recovery_fields:
                raise UnsafeOperation("error instances only allow account credentials and proxy recovery")
        if instance.strategy_progress.stage is StrategyStage.HOLDING:
            raise UnsafeOperation("resume and close the active mock pair before changing configuration")

        current_material = self.vault.get(instance_id)
        original_material = current_material
        credentials = request.credentials
        proxy = request.proxy
        if (
            (credentials is not None or proxy is not None)
            and current_material is None
            and (credentials is None or proxy is None)
        ):
            raise ValidationFailed("credentials and proxy are both required when no stored material exists")

        next_material = current_material
        api_key_tail = instance.api_key_tail
        proxy_snapshot = instance.proxy
        if credentials is not None or proxy is not None:
            normalized_proxy: str | None = None
            if proxy is not None:
                host = "不使用代理"
                if proxy.type is not ProxyType.NONE:
                    assert proxy.url is not None
                    try:
                        host = proxy_host(proxy.type, proxy.url.get_secret_value())
                        normalized_proxy = normalize_proxy_url(proxy.type, proxy.url.get_secret_value())
                    except ProxyValidationError as exc:
                        raise ValidationFailed(str(exc)) from exc
                proxy_snapshot = ProxySnapshot(type=proxy.type, host=host)
            if current_material is None:
                assert credentials is not None and proxy is not None
                current_material = CredentialMaterial(
                    api_key=credentials.api_key,
                    api_secret=credentials.api_secret,
                    passphrase=credentials.passphrase,
                    proxy_url=SecretStr(normalized_proxy) if normalized_proxy is not None else None,
                )
            if credentials is not None:
                api_key = credentials.api_key.get_secret_value()
                api_key_tail = api_key[-4:].upper().rjust(4, "-")

            next_material = CredentialMaterial(
                api_key=credentials.api_key if credentials else current_material.api_key,
                api_secret=credentials.api_secret if credentials else current_material.api_secret,
                passphrase=credentials.passphrase if credentials else current_material.passphrase,
                proxy_url=(
                    SecretStr(normalized_proxy) if normalized_proxy is not None else None
                ) if proxy is not None else current_material.proxy_url,
            )

        name = request.name.strip() if request.name is not None else instance.name
        account_tag = (
            request.account_tag.strip() or "未分组" if request.account_tag is not None else instance.account_tag
        )
        if request.cycle_target is not None and request.cycle_target < instance.cycle.completed:
            raise ValidationFailed("cycle target cannot be lower than completed cycles")
        strategy = instance.strategy
        if request.strategy_id is not None:
            strategy = self.get_strategy(request.strategy_id)
        elif request.cycle_target is not None or request.mock_cycle_total_quote is not None:
            legacy_total_quote = (
                request.mock_cycle_total_quote or instance.mock_cycle_total_quote or self.mock_cycle_total_quote
            )
            strategy = self._create_legacy_strategy(
                cycle_target=request.cycle_target or instance.cycle.target,
                legacy_total_quote=legacy_total_quote,
            )
        strategy_changed = strategy.id != instance.strategy_id
        progress = StrategyProgress() if strategy_changed else instance.strategy_progress
        projected_instance = instance.model_copy(
            update={"strategy_id": strategy.id, "strategy": strategy, "strategy_progress": progress},
            deep=True,
        )
        remaining_estimate = estimate_rounds(strategy, target_progress_quote(projected_instance)).maximum
        cycle = instance.cycle.model_copy(update={"target": max(1, instance.cycle.completed + remaining_estimate)})
        updated = instance.model_copy(
            update={
                "name": name,
                "account_tag": account_tag,
                "api_key_tail": api_key_tail,
                "proxy": proxy_snapshot,
                "cycle": cycle,
                "strategy_id": strategy.id,
                "strategy": strategy,
                "strategy_progress": progress,
                "mock_cycle_total_quote": (
                    request.mock_cycle_total_quote
                    if self.adapter == "mock" and request.mock_cycle_total_quote is not None
                    else instance.mock_cycle_total_quote
                    if self.adapter == "mock"
                    else None
                ),
                "history_start_at_ms": (
                    request.history_start_at_ms
                    if "history_start_at_ms" in request.model_fields_set
                    else instance.history_start_at_ms
                ),
                "phase": "策略已切换，等待启动" if strategy_changed else "配置已更新，等待验证",
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        updated = self._with_funding(updated)
        material_changed = credentials is not None or proxy is not None
        if material_changed and next_material is not None:
            self.vault.put(instance_id, next_material)
        try:
            self.repository.replace(updated)
        except Exception:
            if material_changed:
                if original_material is None:
                    self.vault.remove(instance_id)
                else:
                    self.vault.put(instance_id, original_material)
            raise
        self._append_log(instance_id, LogLevel.INFO, "账号配置已更新")
        return updated

    def reset_telemetry_snapshot(self, instance_id: str, reason: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        updated = instance.model_copy(
            update={
                "phase": "历史口径已变更，等待重新扫描",
                "wallet": WalletSnapshot(),
                "volume": VolumeSnapshot(),
                "exposure": ExposureSnapshot(),
                "proxy": instance.proxy.model_copy(
                    update={
                        "location": "待检测",
                        "latency_ms": None,
                        "status": ProxyStatus.UNCHECKED,
                    }
                ),
                "runtime": RuntimeHealthSnapshot(),
                "updated_at": "尚未同步",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(instance_id, LogLevel.INFO, f"只读遥测已重置：{reason}")
        return updated

    def apply_telemetry(
        self,
        instance_id: str,
        telemetry: AccountTelemetry,
        *,
        poll_started_at_ms: int | None = None,
        poll_completed_at_ms: int | None = None,
        poll_duration_ms: int | None = None,
        strategy_generated_volume_quote: Decimal | None = None,
    ) -> AccountInstance:
        instance = self.get_instance(instance_id)
        completed_at_ms = poll_completed_at_ms or time.time_ns() // 1_000_000
        protected_error = (
            instance.status is InstanceStatus.ERROR and instance.strategy_progress.system_pause_reason is not None
        )
        recovered = instance.status in {InstanceStatus.ERROR, InstanceStatus.WARNING} and not protected_error
        status = InstanceStatus.STOPPED if recovered else instance.status
        phase = instance.phase if protected_error else "连接已恢复，等待人工启动" if recovered else telemetry.phase
        log_count = int(telemetry.activity_log is not None) + int(recovered)
        strategy_progress = instance.strategy_progress
        if (
            instance.strategy.target_mode is StrategyTargetMode.INCREMENTAL
            and strategy_generated_volume_quote is not None
        ):
            strategy_progress = strategy_progress.model_copy(
                update={"generated_volume_quote": max(Decimal(0), strategy_generated_volume_quote)}
            )
        updated = instance.model_copy(
            update={
                "status": status,
                "phase": phase,
                "wallet": telemetry.wallet,
                "volume": telemetry.volume,
                "exposure": telemetry.exposure,
                "proxy": instance.proxy.model_copy(
                    update={
                        "latency_ms": telemetry.proxy_latency_ms,
                        "status": telemetry.proxy_status,
                        "location": telemetry.proxy_location,
                    }
                ),
                "cycle": instance.cycle.model_copy(update={"completed": telemetry.cycle_completed}),
                "strategy_progress": strategy_progress,
                "runtime": instance.runtime.model_copy(
                    update={
                        "last_poll_started_at_ms": poll_started_at_ms or completed_at_ms,
                        "last_poll_succeeded_at_ms": completed_at_ms,
                        "last_poll_duration_ms": poll_duration_ms,
                        **({} if protected_error else {"consecutive_failures": 0, "last_error_type": None}),
                    }
                ),
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + log_count,
            },
            deep=True,
        )
        updated = self._with_funding(updated, wallet_known=True)
        self.repository.replace(updated)
        if recovered:
            self._append_log(instance_id, LogLevel.SUCCESS, "遥测连接已恢复；需要人工启动")
        if telemetry.activity_log is not None:
            self._append_log(instance_id, LogLevel.INFO, telemetry.activity_log)
        return updated

    def record_strategy_execution(
        self,
        instance_id: str,
        record: ExecutionRecord,
        *,
        submitted: bool,
    ) -> AccountInstance:
        instance = self.get_instance(instance_id)
        plan = record.plan
        if record.status is CycleExecutionStatus.OPENED:
            if not submitted or instance.strategy_progress.active_cycle_id == plan.cycle_id:
                return instance
            next_action_at_ms = record.updated_at_ms + plan.position_hold_seconds * 1_000
            ratio = plan.eth_short_quote / plan.btc_long_quote
            progress = instance.strategy_progress.model_copy(
                update={
                    "stage": StrategyStage.HOLDING,
                    "next_action_at_ms": next_action_at_ms,
                    "active_cycle_id": plan.cycle_id,
                    "last_eth_ratio": ratio,
                    "last_allocation_version": plan.allocation_version,
                }
            )
            updated = instance.model_copy(
                update={
                    "phase": "BTC 多 / ETH 空已开仓，等待平仓",
                    "exposure": ExposureSnapshot(
                        btc_long=float(plan.btc_long_quote),
                        eth_short=float(plan.eth_short_quote),
                    ),
                    "cycle": instance.cycle.model_copy(
                        update={"next_action_at": _delay_label(plan.position_hold_seconds, "平仓")}
                    ),
                    "strategy_progress": progress,
                    "updated_at": "刚刚",
                    "unread_logs": instance.unread_logs + 1,
                },
                deep=True,
            )
            self.repository.replace(updated)
            residual = "；本轮按目标残差收尾" if plan.sizing_mode == "residual_finish" else ""
            self._append_log(
                instance_id,
                LogLevel.SUCCESS,
                f"周期 {plan.sequence} 已开仓：BTC {plan.btc_long_quote} + ETH {plan.eth_short_quote} USDT；"
                f"{plan.position_hold_seconds} 秒后平仓{residual}",
            )
            return updated

        if record.status is not CycleExecutionStatus.COMPLETED:
            return instance
        if not submitted or instance.strategy_progress.active_cycle_id != plan.cycle_id:
            return instance
        generated = instance.strategy_progress.generated_volume_quote
        if (
            instance.strategy.target_mode is StrategyTargetMode.INCREMENTAL
            and instance.strategy_progress.started_at_ms is None
        ):
            generated += plan.turnover_quote or plan.total_quote * 2
        progress_before_state = instance.strategy_progress.model_copy(update={"generated_volume_quote": generated})
        projected = instance.model_copy(update={"strategy_progress": progress_before_state}, deep=True)
        achieved = target_progress_quote(projected)
        target_reached = achieved >= instance.strategy.target_volume_quote
        next_action_at_ms = None if target_reached else record.updated_at_ms + plan.round_interval_seconds * 1_000
        progress = progress_before_state.model_copy(
            update={
                "generated_volume_quote": generated,
                "stage": StrategyStage.COMPLETE if target_reached else StrategyStage.COOLDOWN,
                "next_action_at_ms": next_action_at_ms,
                "active_cycle_id": None,
            }
        )
        updated = instance.model_copy(
            update={
                "status": InstanceStatus.STOPPED if target_reached else instance.status,
                "phase": "目标交易量已完成" if target_reached else "本轮已平仓，等待下一轮",
                "exposure": ExposureSnapshot(),
                "cycle": instance.cycle.model_copy(
                    update={
                        "completed": plan.sequence,
                        "next_action_at": (
                            None if target_reached else _delay_label(plan.round_interval_seconds, "下一轮")
                        ),
                    }
                ),
                "strategy_progress": progress,
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(
            instance_id,
            LogLevel.SUCCESS,
            f"周期 {plan.sequence} 已平仓；本轮贡献 {plan.turnover_quote} USDT，"
            f"策略进度 {achieved}/{instance.strategy.target_volume_quote} USDT",
        )
        if target_reached:
            self._append_log(instance_id, LogLevel.SUCCESS, "已精确达到目标交易量；运行已停止")
        return updated

    def set_volume_completeness(self, instance_id: str, complete: bool) -> AccountInstance:
        instance = self.get_instance(instance_id)
        updated = instance.model_copy(
            update={"volume": instance.volume.model_copy(update={"complete": complete})},
            deep=True,
        )
        self.repository.replace(updated)
        return updated

    def record_runtime_failure(
        self,
        instance_id: str,
        failure_type: str,
        *,
        poll_started_at_ms: int | None = None,
        poll_failed_at_ms: int | None = None,
        poll_duration_ms: int | None = None,
    ) -> AccountInstance:
        instance = self.get_instance(instance_id)
        failed_at_ms = poll_failed_at_ms or time.time_ns() // 1_000_000
        legacy_telemetry_error = (
            instance.status is InstanceStatus.ERROR
            and instance.strategy_progress.system_pause_reason is None
            and instance.phase.startswith("数据同步失败 (")
        )
        if instance.status is InstanceStatus.STOPPED or legacy_telemetry_error:
            status = InstanceStatus.STOPPED
            phase = f"已停止；数据待核验 ({failure_type})"
        elif instance.status is InstanceStatus.PAUSED:
            status = InstanceStatus.PAUSED
            phase = f"已暂停；数据待核验 ({failure_type})"
        elif instance.status in {InstanceStatus.RUNNING, InstanceStatus.WARNING}:
            status = InstanceStatus.WARNING
            phase = f"运行已安全暂停；数据待核验 ({failure_type})"
        else:
            # Protected execution/cancellation errors must never be downgraded
            # by a later read-only telemetry failure.
            status = InstanceStatus.ERROR
            phase = instance.phase
        should_log = (
            instance.status is not status
            or instance.phase != phase
            or instance.runtime.last_error_type != failure_type
        )
        updated = instance.model_copy(
            update={
                "status": status,
                "phase": phase,
                "proxy": instance.proxy.model_copy(update={"status": ProxyStatus.DEGRADED}),
                "cycle": instance.cycle.model_copy(update={"next_action_at": None}),
                "runtime": instance.runtime.model_copy(
                    update={
                        "last_poll_started_at_ms": poll_started_at_ms or failed_at_ms,
                        "last_poll_failed_at_ms": failed_at_ms,
                        "last_poll_duration_ms": poll_duration_ms,
                        "consecutive_failures": instance.runtime.consecutive_failures + 1,
                        "last_error_type": failure_type,
                    }
                ),
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + int(should_log),
            },
            deep=True,
        )
        self.repository.replace(updated)
        if should_log:
            self._append_log(instance_id, LogLevel.ERROR, f"遥测失败：{failure_type}")
        return updated

    def record_execution_failure(self, instance_id: str, status: str, reason: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        phase = f"配对执行{status}: {reason}"
        updated = instance.model_copy(
            update={
                "status": InstanceStatus.ERROR,
                "phase": phase,
                "cycle": instance.cycle.model_copy(update={"next_action_at": None}),
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(instance_id, LogLevel.ERROR, f"配对执行 {status}：{reason}")
        return updated

    def stop_at_cycle_target(self, instance_id: str) -> AccountInstance:
        instance = self.get_instance(instance_id)
        if target_progress_quote(instance) < instance.strategy.target_volume_quote:
            raise ValidationFailed("volume target has not been reached")
        if instance.status is InstanceStatus.STOPPED and instance.phase == "目标交易量已完成":
            return instance
        updated = instance.model_copy(
            update={
                "status": InstanceStatus.STOPPED,
                "phase": "目标交易量已完成",
                "cycle": instance.cycle.model_copy(update={"next_action_at": None}),
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(instance_id, LogLevel.SUCCESS, "已达到目标交易量；运行已停止")
        return updated

    def reconcile_after_restart(self) -> int:
        reconciled = 0
        for instance in self.repository.list():
            if instance.status in {InstanceStatus.STOPPED, InstanceStatus.ERROR}:
                continue
            reconciled += 1
            updated = instance.model_copy(
                update={
                    "status": InstanceStatus.STOPPED,
                    "phase": "服务已重启，等待人工启动",
                    "cycle": instance.cycle.model_copy(update={"next_action_at": None}),
                    "updated_at": "刚刚",
                    "unread_logs": instance.unread_logs + 1,
                },
                deep=True,
            )
            self.repository.replace(updated)
            self._append_log(instance.id, LogLevel.WARN, "检测到服务重启；需要人工启动")
        return reconciled

    def logs(self, instance_id: str, limit: int) -> list[LogLine]:
        instance = self.get_instance(instance_id)
        lines = self.repository.read_logs(instance_id, limit)
        if instance.unread_logs:
            self.repository.replace(instance.model_copy(update={"unread_logs": 0}, deep=True))
        return lines

    def record_refresh_success(self, instance_id: str) -> AccountInstance:
        """Record an explicit user-triggered refresh without logging background polls."""
        instance = self.get_instance(instance_id)
        updated = instance.model_copy(
            update={"unread_logs": instance.unread_logs + 1},
            deep=True,
        )
        self.repository.replace(updated)
        self._append_log(instance_id, LogLevel.SUCCESS, "刷新成功：价格、钱包与仓位已同步")
        return updated

    def record_campaign_progress(self, instance_id: str, event: Mapping[str, object]) -> None:
        """Project a safe, durable Campaign event into the account log stream.

        The worker has already persisted the event before calling here.  This is
        strictly an observability projection and cannot alter execution state.
        """
        self.get_instance(instance_id)
        rendered = campaign_event_log(event)
        if rendered is None:
            return
        level, message = rendered
        self._append_log(instance_id, level, message)

    def clear_logs(self, instance_id: str) -> None:
        instance = self.get_instance(instance_id)
        self.repository.clear_logs(instance_id)
        if instance.unread_logs:
            self.repository.replace(instance.model_copy(update={"unread_logs": 0}, deep=True))

    def log_updates(self, instance_id: str, limit: int, after: str | None) -> LogBatch:
        instance = self.get_instance(instance_id)
        window = self.repository.read_logs(instance_id, 500)
        reset = False
        if after is None:
            lines = window[-limit:]
        else:
            cursor_index = next((index for index, line in enumerate(window) if line.id == after), None)
            if cursor_index is None:
                lines = window[-limit:]
                reset = True
            else:
                lines = window[cursor_index + 1 : cursor_index + 1 + limit]
        if instance.unread_logs:
            self.repository.replace(instance.model_copy(update={"unread_logs": 0}, deep=True))
        cursor = lines[-1].id if lines else (None if reset else after)
        return LogBatch(lines=lines, cursor=cursor, reset=reset)

    @staticmethod
    def validate_global_stop_confirmation(confirmation: str) -> None:
        if confirmation != "STOP ALL":
            raise UnsafeOperation("confirmation mismatch; expected exactly: STOP ALL")

    def delete_instance(self, instance_id: str) -> None:
        instance = self.get_instance(instance_id)
        if instance.status is not InstanceStatus.STOPPED:
            raise UnsafeOperation("stop the instance before deleting it")
        if instance.strategy_progress.stage is StrategyStage.HOLDING:
            raise UnsafeOperation("resume and close the active mock pair before deleting the instance")
        self.repository.delete(instance_id)
        self.vault.remove(instance_id)

    def _append_log(self, instance_id: str, level: LogLevel, message: str) -> None:
        self.repository.append_log(
            instance_id,
            LogLine(id=uuid4().hex, timestamp=_now(), level=level, message=message),
        )

    def _reconcile_strategy_catalog(self) -> None:
        strategies = {strategy.id: strategy for strategy in self.repository.list_strategies()}
        projections: list[AccountInstance] = []
        for instance in self.repository.list():
            canonical = strategies.get(instance.strategy.id)
            if canonical is None:
                canonical = self.repository.create_strategy(instance.strategy)
                strategies[canonical.id] = canonical
            projected = instance.model_copy(
                update={"strategy_id": canonical.id, "strategy": canonical},
                deep=True,
            )
            projected = self._with_funding(projected)
            if projected != instance:
                projections.append(projected)
        if projections:
            self.repository.replace_many(projections)
        if not strategies:
            self.repository.create_strategy(default_volume_strategy())

    def _default_strategy(self) -> VolumeStrategy:
        owner = current_owner_user_id() or LEGACY_OWNER_USER_ID
        strategy_id = "strategy-default" if owner == LEGACY_OWNER_USER_ID else f"strategy-default-{owner}"
        strategy = self.repository.get_strategy(strategy_id)
        if strategy is not None:
            return strategy
        strategies = self.repository.list_strategies()
        if strategies:
            return strategies[0]
        return self.repository.create_strategy(
            default_volume_strategy().model_copy(update={"id": strategy_id, "owner_user_id": owner})
        )

    def _create_legacy_strategy(self, *, cycle_target: int, legacy_total_quote: Decimal) -> VolumeStrategy:
        round_turnover = legacy_total_quote * Decimal(2)
        strategy = VolumeStrategy(
            id=f"strategy-{uuid4().hex[:10]}",
            owner_user_id=current_owner_user_id() or LEGACY_OWNER_USER_ID,
            name="兼容固定轮次策略",
            target_volume_quote=round_turnover * cycle_target,
            round_turnover_quote_min=round_turnover,
            round_turnover_quote_max=round_turnover,
            position_hold_min_seconds=0,
            position_hold_max_seconds=0,
            round_interval_min_seconds=0,
            round_interval_max_seconds=0,
        )
        return self.repository.create_strategy(strategy)

    @staticmethod
    def _ensure_configurable(instance: AccountInstance, operation: str) -> None:
        if instance.status is not InstanceStatus.STOPPED:
            raise UnsafeOperation(f"stop instance {instance.id!r} before {operation}")
        has_open_pair = (
            instance.strategy_progress.stage is StrategyStage.HOLDING
            or instance.exposure.btc_long != 0
            or instance.exposure.eth_short != 0
        )
        if has_open_pair:
            raise UnsafeOperation(f"close the active pair on instance {instance.id!r} before {operation}")

    def _strategy_projection(
        self,
        instance: AccountInstance,
        strategy: VolumeStrategy,
        *,
        reset_progress: bool,
    ) -> AccountInstance:
        if reset_progress:
            progress = StrategyProgress()
        else:
            projected = instance.model_copy(update={"strategy": strategy}, deep=True)
            achieved = target_progress_quote(projected, strategy)
            progress = instance.strategy_progress.model_copy(
                update={
                    "stage": (
                        StrategyStage.COMPLETE if achieved >= strategy.target_volume_quote else StrategyStage.IDLE
                    ),
                    "next_action_at_ms": None,
                    "active_cycle_id": None,
                }
            )
        projected = instance.model_copy(
            update={"strategy_id": strategy.id, "strategy": strategy, "strategy_progress": progress},
            deep=True,
        )
        achieved = target_progress_quote(projected)
        remaining_rounds = estimate_rounds(strategy, achieved).maximum
        cycle = instance.cycle.model_copy(
            update={
                "target": max(1, instance.cycle.completed + remaining_rounds),
                "next_action_at": None,
            }
        )
        complete = achieved >= strategy.target_volume_quote
        updated = instance.model_copy(
            update={
                "strategy_id": strategy.id,
                "strategy": strategy,
                "strategy_progress": progress,
                "cycle": cycle,
                "phase": "目标交易量已完成" if complete else "策略已更新，等待启动",
                "updated_at": "刚刚",
                "unread_logs": instance.unread_logs + 1,
            },
            deep=True,
        )
        return self._with_funding(updated)

    @staticmethod
    def _with_funding(instance: AccountInstance, *, wallet_known: bool | None = None) -> AccountInstance:
        known = (
            wallet_known
            if wallet_known is not None
            else instance.runtime.last_poll_succeeded_at_ms is not None
            or instance.wallet.equity > 0
            or instance.wallet.available > 0
        )
        preflight = funding_preflight(
            instance.strategy,
            instance.wallet.available,
            wallet_known=known,
        )
        return instance.model_copy(update={"funding_preflight": preflight}, deep=True)


def _delay_label(seconds: int, action: str) -> str:
    return f"{seconds}s 后{action}"

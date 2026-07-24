from __future__ import annotations

import time
from decimal import Decimal
from uuid import uuid4

from pydantic import SecretStr

from .models import (
    AccountInstance,
    CreateInstanceRequest,
    CycleSnapshot,
    FundingPreflightStatus,
    InstanceAction,
    InstanceStatus,
    LogLevel,
    ProxySnapshot,
    ProxyType,
    StrategyProgress,
    StrategyStage,
    StrategyTargetMode,
    WalletSnapshot,
)
from .ownership import LEGACY_OWNER_USER_ID, current_owner_user_id
from .proxy import ProxyValidationError, normalize_proxy_url, proxy_host
from .service_errors import (
    UnsafeOperation,
    ValidationFailed,
)
from .strategy import estimate_rounds
from .vault import CredentialMaterial


class ServiceInstancesMixin:
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

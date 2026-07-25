from __future__ import annotations

from pydantic import SecretStr

from fleet_api.accounts.proxy import ProxyValidationError, normalize_proxy_url, proxy_host
from fleet_api.auth.vault import CredentialMaterial
from fleet_api.models import (
    AccountInstance,
    ExposureSnapshot,
    InstanceStatus,
    LogLevel,
    ProxySnapshot,
    ProxyStatus,
    ProxyType,
    RuntimeHealthSnapshot,
    StrategyProgress,
    StrategyStage,
    UpdateInstanceRequest,
    VolumeSnapshot,
    WalletSnapshot,
)
from fleet_api.services.control.service_errors import (
    UnsafeOperation,
    ValidationFailed,
)
from fleet_api.strategy.strategy import estimate_rounds, target_progress_quote


class ServiceConfigMixin:
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
                proxy_url=(SecretStr(normalized_proxy) if normalized_proxy is not None else None)
                if proxy is not None
                else current_material.proxy_url,
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

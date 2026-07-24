from __future__ import annotations

from weex_cli.config import Credentials, Settings
from weex_cli.gateway import WeexGateway
from weex_cli.live_profile import LiveProfile

from .campaign_helpers import _normalize_proxy_url
from .config import ControlPlaneSettings
from .vault import CredentialMaterial


def build_live_profile_gateway(
    control_settings: ControlPlaneSettings,
    material: CredentialMaterial,
) -> tuple[LiveProfile, WeexGateway]:
    settings = Settings(
        credentials=Credentials(
            api_key=material.api_key.get_secret_value(),
            api_secret=material.api_secret.get_secret_value(),
            passphrase=material.passphrase.get_secret_value(),
        ),
        default_mode="live",
        live_trading_enabled=True,
        timeout_ms=control_settings.weex_request_timeout_ms,
        enable_rate_limit=True,
    )
    profile = LiveProfile(
        path=control_settings.campaign_data_directory / "control-plane-live.toml",
        settings=settings,
        proxy_url=_normalize_proxy_url(
            material.proxy_url.get_secret_value() if material.proxy_url is not None else None
        ),
        allow_live_mutations=True,
        post_only_only=True,
    )
    profile.require_maker_execution()
    return profile, WeexGateway(settings, proxy_url=profile.proxy_url)

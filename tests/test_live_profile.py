from __future__ import annotations

from pathlib import Path

import pytest

from weex_cli.core.errors import ConfigurationError, SafetyError
from weex_cli.live_profile import load_live_profile


def write_profile(root: Path, *, proxy: str = "127.0.0.1:8080:user:password") -> Path:
    (root / "pyproject.toml").write_text("[project]\nname='profile-test'\nversion='0'\n", encoding="utf-8")
    path = root / "live.toml"
    path.write_text(
        f"""
[weex]
mode = "live"

[credentials]
api_key = "key-value"
api_secret = "secret-value"
passphrase = "pass-value"

[network]
scheme = "http"
proxy = "{proxy}"

[safety]
allow_live_mutations = true
post_only_only = true
""",
        encoding="utf-8",
    )
    return path


def test_profile_loads_credentials_and_proxy_without_using_it_as_live_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_profile(tmp_path)

    profile = load_live_profile(path, environ={})

    assert profile.settings.credentials.configured is True
    assert profile.settings.live_trading_enabled is False
    assert profile.proxy_url == "http://user:password@127.0.0.1:8080"
    assert "secret-value" not in repr(profile)
    assert "password" not in repr(profile)
    profile.require_maker_execution()


def test_profile_rejects_invalid_webshare_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_profile(tmp_path, proxy="127.0.0.1:8080:user")

    with pytest.raises(ConfigurationError, match="host:port:username:password"):
        load_live_profile(path, environ={})


def test_profile_safety_is_an_additional_deny_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_profile(tmp_path)
    text = path.read_text(encoding="utf-8").replace("allow_live_mutations = true", "allow_live_mutations = false")
    path.write_text(text, encoding="utf-8")
    profile = load_live_profile(path, environ={"WEEX_LIVE_TRADING_ENABLED": "true"})

    with pytest.raises(SafetyError, match="profile blocks mutations"):
        profile.require_maker_execution()

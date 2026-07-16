from __future__ import annotations

import pytest

from weex_cli.config import Settings, normalize_mode
from weex_cli.errors import ConfigurationError


def test_settings_loads_canonical_credentials() -> None:
    settings = Settings.load(
        environ={
            "WEEX_API_KEY": "key",
            "WEEX_API_SECRET": "secret",
            "WEEX_API_PASSPHRASE": "pass",
            "WEEX_DEFAULT_MODE": "sim",
            "WEEX_LIVE_TRADING_ENABLED": "yes",
            "WEEX_TIMEOUT_MS": "20000",
        }
    )
    assert settings.credentials.configured is True
    assert settings.default_mode == "demo"
    assert settings.live_trading_enabled is True
    assert settings.timeout_ms == 20_000


def test_settings_supports_legacy_variable_aliases_only_in_selected_env() -> None:
    settings = Settings.load(
        environ={
            "WEEX_API_KEY": "key",
            "WEEX_SECRET_KEY": "secret",
            "WEEX_PASSPHRASE": "pass",
        }
    )
    assert settings.credentials.api_secret == "secret"
    assert settings.credentials.passphrase == "pass"


def test_require_credentials_lists_expected_names() -> None:
    with pytest.raises(ConfigurationError, match="WEEX_API_SECRET"):
        Settings.load(environ={}).require_credentials()


@pytest.mark.parametrize("value, expected", [("paper", "demo"), ("prod", "live"), ("live", "live")])
def test_normalize_mode_aliases(value: str, expected: str) -> None:
    assert normalize_mode(value) == expected


@pytest.mark.parametrize("values", [{"WEEX_TIMEOUT_MS": "bad"}, {"WEEX_TIMEOUT_MS": "999"}])
def test_invalid_timeout_is_rejected(values: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError):
        Settings.load(environ=values)


def test_automatic_env_load_stays_within_project(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    nested = project / "tools"
    nested.mkdir(parents=True)
    (project / "pyproject.toml").write_text('[project]\nname = "test"\n', encoding="utf-8")
    (project / ".env").write_text(
        "WEEX_API_KEY=project-key\nWEEX_API_SECRET=project-secret\nWEEX_API_PASSPHRASE=project-pass\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(nested)
    for name in ("WEEX_API_KEY", "WEEX_API_SECRET", "WEEX_API_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.load()

    assert settings.credentials.configured is True
    assert settings.env_file == str((project / ".env").resolve())


def test_automatic_env_load_never_crosses_project_root(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text('[project]\nname = "test"\n', encoding="utf-8")
    (tmp_path / ".env").write_text(
        "WEEX_API_KEY=parent-key\nWEEX_API_SECRET=parent-secret\nWEEX_API_PASSPHRASE=parent-pass\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    for name in ("WEEX_API_KEY", "WEEX_API_SECRET", "WEEX_API_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.load()

    assert settings.credentials.configured is False
    assert settings.env_file is None

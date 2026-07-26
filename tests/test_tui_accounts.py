from __future__ import annotations

from pathlib import Path

import pytest

from weex_cli.core.errors import ConfigurationError
from weex_cli.tui.accounts import (
    DEFAULT_ACCOUNT_FILE,
    AccountInUseError,
    AccountLease,
    load_tui_account_catalog,
)

VALID_TOML = """\
schema_version = 1

[safety]
allow_live_mutations = true
post_only_only = true

[beta]
url = "https://beta.private.test/api/v1/hedge-ratio?token=private-beta-token"

[defaults]
passphrase = "private-passphrase"

[[accounts]]
name = "Account 01"
enabled = true
api_key = "key-1234"
api_secret = "private-secret"
proxy_scheme = "http"
proxy = "82.21.23.245:6009:user:password"
"""


def test_default_catalog_path_uses_ignored_config_instance() -> None:
    assert Path("config/tui-accounts.toml") == DEFAULT_ACCOUNT_FILE


def write_catalog(root: Path, text: str = VALID_TOML, *, mode: int = 0o600) -> Path:
    (root / "pyproject.toml").write_text("[project]\nname='test'\nversion='0'\n", encoding="utf-8")
    path = root / "accounts.toml"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def test_loads_valid_catalog_and_never_represents_secrets(tmp_path: Path) -> None:
    path = write_catalog(tmp_path)

    catalog = load_tui_account_catalog(path, start=tmp_path)
    account = catalog.accounts[0]

    assert catalog.safety.allow_live_mutations is True
    assert catalog.beta_url.startswith("https://beta.private.test/")
    assert account.api_key_tail == "1234"
    assert account.proxy_host == "82.21.23.245"
    assert account.proxy_url == "http://user:password@82.21.23.245:6009"
    assert account.account_id.startswith("account-")
    rendered = repr(account)
    assert "private-secret" not in rendered
    assert "private-passphrase" not in rendered
    assert "password" not in rendered
    assert "private-beta-token" not in repr(catalog)


@pytest.mark.parametrize(
    "beta_table",
    [
        "",
        '[beta]\nurl = ""\n',
        '[beta]\nurl = "file:///tmp/beta.json"\n',
        '[beta]\nurl = "https://beta.private.test/path#secret"\n',
    ],
)
def test_rejects_missing_or_invalid_beta_endpoint(tmp_path: Path, beta_table: str) -> None:
    start = VALID_TOML.index("[beta]")
    end = VALID_TOML.index("[defaults]")
    text = VALID_TOML[:start] + beta_table + "\n" + VALID_TOML[end:]
    path = write_catalog(tmp_path, text)

    with pytest.raises(ConfigurationError, match=r"beta|\[beta\]"):
        load_tui_account_catalog(path, start=tmp_path)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ('name = "Account 01"', "names must be unique"),
        ('name = ""', "requires name"),
        ('passphrase = ""', "requires passphrase"),
        ('proxy = "host:not-a-port:user:password"', "port must be an integer"),
        ('proxy = "host:70000:user:password"', "outside 1-65535"),
    ],
)
def test_rejects_invalid_account_rows(tmp_path: Path, replacement: str, message: str) -> None:
    text = VALID_TOML
    if message == "names must be unique":
        text += "\n[[accounts]]" + VALID_TOML.split("[[accounts]]", 1)[1]
    elif message == "requires name":
        text = text.replace('name = "Account 01"', replacement)
    elif message == "requires passphrase":
        text = text.replace('[defaults]\npassphrase = "private-passphrase"\n', "")
    elif "proxy" in replacement:
        text = text.replace('proxy = "82.21.23.245:6009:user:password"', replacement)
    path = write_catalog(tmp_path, text)

    with pytest.raises(ConfigurationError, match=message):
        load_tui_account_catalog(path, start=tmp_path)


def test_multiple_accounts_need_only_unique_names(tmp_path: Path) -> None:
    second = VALID_TOML.split("[[accounts]]", 1)[1].replace('name = "Account 01"', 'name = "账户 二"')
    path = write_catalog(tmp_path, VALID_TOML + "\n[[accounts]]" + second)

    loaded = load_tui_account_catalog(path, start=tmp_path)

    assert [account.name for account in loaded.accounts] == ["Account 01", "账户 二"]
    assert len({account.account_id for account in loaded.accounts}) == 2


def test_legacy_id_is_ignored_and_does_not_change_runtime_identity(tmp_path: Path) -> None:
    legacy = VALID_TOML.replace('name = "Account 01"', 'id = "old-manual-id"\nname = "Account 01"')
    modern_path = write_catalog(tmp_path, VALID_TOML)
    modern_id = load_tui_account_catalog(modern_path, start=tmp_path).accounts[0].account_id
    modern_path.write_text(legacy, encoding="utf-8")
    modern_path.chmod(0o600)

    legacy_id = load_tui_account_catalog(modern_path, start=tmp_path).accounts[0].account_id

    assert legacy_id == modern_id


def test_account_passphrase_overrides_global_default(tmp_path: Path) -> None:
    text = VALID_TOML.replace(
        'api_secret = "private-secret"',
        'api_secret = "private-secret"\npassphrase = "account-specific-passphrase"',
    )
    path = write_catalog(tmp_path, text)

    loaded = load_tui_account_catalog(path, start=tmp_path)

    assert loaded.accounts[0].passphrase == "account-specific-passphrase"


def test_global_passphrase_is_inherited_by_every_account(tmp_path: Path) -> None:
    second = VALID_TOML.split("[[accounts]]", 1)[1].replace('name = "Account 01"', 'name = "Account 02"')
    path = write_catalog(tmp_path, VALID_TOML + "\n[[accounts]]" + second)

    loaded = load_tui_account_catalog(path, start=tmp_path)

    assert [account.passphrase for account in loaded.accounts] == [
        "private-passphrase",
        "private-passphrase",
    ]


def test_legacy_per_account_passphrase_still_loads(tmp_path: Path) -> None:
    text = VALID_TOML.replace('[defaults]\npassphrase = "private-passphrase"\n\n', "").replace(
        'api_secret = "private-secret"',
        'api_secret = "private-secret"\npassphrase = "legacy-passphrase"',
    )
    path = write_catalog(tmp_path, text)

    loaded = load_tui_account_catalog(path, start=tmp_path)

    assert loaded.accounts[0].passphrase == "legacy-passphrase"


def test_defaults_must_be_a_table(tmp_path: Path) -> None:
    text = VALID_TOML.replace("schema_version = 1\n", 'schema_version = 1\ndefaults = "invalid"\n').replace(
        '[defaults]\npassphrase = "private-passphrase"\n\n', ""
    )
    path = write_catalog(tmp_path, text)

    with pytest.raises(ConfigurationError, match=r"\[defaults\] must be a table"):
        load_tui_account_catalog(path, start=tmp_path)


def test_rejects_external_symlink_and_broad_permissions(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    external = tmp_path / "outside.toml"
    external.write_text(VALID_TOML, encoding="utf-8")
    external.chmod(0o600)
    (root / "pyproject.toml").write_text("[project]\nname='test'\nversion='0'\n", encoding="utf-8")
    link = root / "accounts.toml"
    link.symlink_to(external)

    with pytest.raises(ConfigurationError, match="symbolic link"):
        load_tui_account_catalog(link, start=root)

    link.unlink()
    path = write_catalog(root, mode=0o640)
    with pytest.raises(ConfigurationError, match="0600"):
        load_tui_account_catalog(path, start=root)

    other = tmp_path / "other"
    other.mkdir()
    outside = write_catalog(other)
    with pytest.raises(ConfigurationError, match="inside the current project root"):
        load_tui_account_catalog(outside, start=root)


def test_account_leases_block_only_the_same_account(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    first = AccountLease("account-01", runtime).acquire()
    other = AccountLease("account-02", runtime).acquire()
    try:
        assert AccountLease.is_locked("account-01", runtime) is True
        with pytest.raises(AccountInUseError, match="already in use"):
            AccountLease("account-01", runtime).acquire()
        assert first.path.stat().st_mode & 0o777 == 0o600
    finally:
        other.release()
        first.release()

    assert AccountLease.is_locked("account-01", runtime) is False

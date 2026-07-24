from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path


def _write_build(source: Path, asset: str) -> None:
    (source / "assets").mkdir(parents=True, exist_ok=True)
    (source / "index.html").write_text(f'<script src="/assets/{asset}"></script>', encoding="utf-8")
    (source / "assets" / asset).write_text("console.log('release')", encoding="utf-8")


def _run(script: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("zsh")
    assert shell is not None, "zsh must be installed to exercise Fleet release scripts"
    return subprocess.run(
        [shell, str(script)],
        check=True,
        env=environment,
        text=True,
        capture_output=True,
    )


def _short_unix_socket() -> Path:
    path = Path(f"/tmp/wfa-{os.getpid()}-{time.time_ns()}.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
    finally:
        listener.close()
    return path


def test_web_release_publish_and_rollback_only_switches_static_symlink(tmp_path: Path) -> None:
    control_center = Path(__file__).parents[2]
    deploy = control_center / "scripts" / "macos" / "deploy-web-release.zsh"
    rollback = control_center / "scripts" / "macos" / "rollback-web-release.zsh"
    source = tmp_path / "dist"
    release_root = tmp_path / "runtime"
    environment = {
        **os.environ,
        "FLEET_WEB_DIST_SOURCE": str(source),
        "FLEET_RELEASE_ROOT": str(release_root),
    }

    _write_build(source, "index-firsthash.js")
    first = _run(deploy, environment)
    assert "web release active:" in first.stdout
    current = release_root / "web-current"
    first_release = current.resolve()
    assert json.loads((first_release / "release.json").read_text(encoding="utf-8"))["release_id"] == first_release.name
    assert not (release_root / "api-restarted").exists()

    time.sleep(1.05)
    (source / "assets" / "index-firsthash.js").unlink()
    _write_build(source, "index-secondhash.js")
    _run(deploy, environment)
    second_release = current.resolve()
    assert second_release != first_release
    assert (first_release / "assets" / "index-firsthash.js").is_file()
    assert (second_release / "assets" / "index-secondhash.js").is_file()

    result = _run(rollback, environment)
    assert "web release rollback active:" in result.stdout
    assert current.resolve() == first_release


def test_service_release_and_launch_agent_scripts_use_stable_paths_without_credentials() -> None:
    control_center = Path(__file__).parents[2]
    deploy = (control_center / "scripts" / "macos" / "deploy-service-release.zsh").read_text(encoding="utf-8")
    installer = (control_center / "scripts" / "macos" / "install-launch-agents.zsh").read_text(encoding="utf-8")
    activate = (control_center / "scripts" / "macos" / "activate-service-release.zsh").read_text(encoding="utf-8")

    assert "service-releases" in deploy
    assert "service-current" in deploy
    assert "service-previous" in deploy
    assert 'contains_credentials": False' in deploy
    assert 'git -C "${repo_root}" archive HEAD src control-center/server/src' in deploy
    assert "src control-center/server/src" in deploy
    assert 'PYTHONPATH="${stage_dir}/src:${stage_dir}/control-center/server/src"' in deploy
    assert "repo_root}/.env" not in deploy
    assert "launchctl" not in deploy

    assert "service-current" in installer
    assert "run-executor.zsh" in installer
    assert "run-api.zsh" in installer
    assert "${release_root}/.env.live" in installer
    assert "FLEET_DB_PATH" in installer
    assert "FLEET_CAMPAIGN_DATA_DIR" in installer
    assert "${release_root}/state" in installer
    assert 'service_root=\\"${service_root:A}\\"' in installer
    assert "Documents" not in installer.split("service_runner =", 1)[1]
    assert "LaunchAgent files were installed but not activated" in installer

    assert "missing stable Live environment file" in activate
    assert "legacy API still owns active Live campaign workers" in activate
    assert "legacy API health cannot prove it has no Live campaign workers" in activate
    assert "liveCampaignActiveWorkerCount" in activate
    assert "liveCampaignWorkerCount" not in activate
    assert "service-current.rollback" in activate
    assert "restored previous API release" in activate
    assert "executor did not pass its local health check" in activate
    assert "--unix-socket" in activate
    assert "wait_for_unload" in activate
    assert "LaunchAgent did not finish unloading" in activate
    assert 'bootout "${domain}/${api_label}"' in activate
    assert 'bootstrap "${domain}" "${api_plist}"' in activate


def test_service_activation_refuses_active_legacy_worker_before_launching_executor(tmp_path: Path) -> None:
    control_center = Path(__file__).parents[2]
    activate = control_center / "scripts" / "macos" / "activate-service-release.zsh"
    release_root = tmp_path / "runtime"
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    socket_path = _short_unix_socket()
    launch_log = tmp_path / "launchctl.log"

    (release_root / "service-current").mkdir(parents=True)
    (release_root / "service-current" / "release.json").write_text("{}", encoding="utf-8")
    (release_root / ".env.live").write_text("# fixture only; no credentials\n", encoding="utf-8")
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    for label in ("com.groove.weex-fleet-executor", "com.groove.weex-fleet-api"):
        (agents / f"{label}.plist").write_text("<plist/>", encoding="utf-8")

    fake_bin.mkdir()
    (fake_bin / "curl").write_text(
        "#!/bin/zsh\n"
        'print -- "{\\"executorConnected\\":false,\\"liveCampaignActiveWorkerCount\\":${FLEET_FAKE_ACTIVE:-0}}"\n',
        encoding="utf-8",
    )
    (fake_bin / "launchctl").write_text(
        '#!/bin/zsh\n[[ "$1" == "print" ]] && exit 1\nprint -r -- "$*" >> "${FLEET_TEST_LAUNCH_LOG}"\n',
        encoding="utf-8",
    )
    (fake_bin / "sleep").write_text("#!/bin/zsh\nexit 0\n", encoding="utf-8")
    for command in fake_bin.iterdir():
        command.chmod(0o700)

    environment = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "FLEET_RELEASE_ROOT": str(release_root),
        "FLEET_EXECUTOR_SOCKET": str(socket_path),
        "FLEET_LEGACY_API_HEALTH_URL": "http://fixture.invalid/api/v1/health",
        "FLEET_TEST_LAUNCH_LOG": str(launch_log),
        "FLEET_FAKE_ACTIVE": "1",
    }
    result = subprocess.run(
        ["/bin/zsh", str(activate)],
        check=False,
        env=environment,
        text=True,
        capture_output=True,
    )
    socket_path.unlink(missing_ok=True)

    assert result.returncode == 1
    assert "legacy API still owns active Live campaign workers" in result.stderr
    assert not launch_log.exists()


def test_service_activation_starts_executor_before_api_after_clear_legacy_check(tmp_path: Path) -> None:
    control_center = Path(__file__).parents[2]
    activate = control_center / "scripts" / "macos" / "activate-service-release.zsh"
    release_root = tmp_path / "runtime"
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    socket_path = _short_unix_socket()
    launch_log = tmp_path / "launchctl.log"

    (release_root / "service-current").mkdir(parents=True)
    (release_root / "service-current" / "release.json").write_text("{}", encoding="utf-8")
    (release_root / ".env.live").write_text("# fixture only; no credentials\n", encoding="utf-8")
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    for label in ("com.groove.weex-fleet-executor", "com.groove.weex-fleet-api"):
        (agents / f"{label}.plist").write_text("<plist/>", encoding="utf-8")

    fake_bin.mkdir()
    (fake_bin / "curl").write_text(
        '#!/bin/zsh\nprint -- \'{"executorConnected":false,"liveCampaignActiveWorkerCount":0}\'\n',
        encoding="utf-8",
    )
    (fake_bin / "launchctl").write_text(
        '#!/bin/zsh\n[[ "$1" == "print" ]] && exit 1\nprint -r -- "$*" >> "${FLEET_TEST_LAUNCH_LOG}"\n',
        encoding="utf-8",
    )
    (fake_bin / "sleep").write_text("#!/bin/zsh\nexit 0\n", encoding="utf-8")
    for command in fake_bin.iterdir():
        command.chmod(0o700)

    result = _run(
        activate,
        {
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "FLEET_RELEASE_ROOT": str(release_root),
            "FLEET_EXECUTOR_SOCKET": str(socket_path),
            "FLEET_LEGACY_API_HEALTH_URL": "http://fixture.invalid/api/v1/health",
            "FLEET_TEST_LAUNCH_LOG": str(launch_log),
        },
    )
    socket_path.unlink(missing_ok=True)

    assert "service release activated:" in result.stdout
    lines = launch_log.read_text(encoding="utf-8").splitlines()
    executor_start = next(
        index for index, line in enumerate(lines) if "executor" in line and line.startswith("bootstrap")
    )
    api_start = next(index for index, line in enumerate(lines) if "api" in line and line.startswith("bootstrap"))
    assert executor_start < api_start

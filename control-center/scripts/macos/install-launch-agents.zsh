#!/bin/zsh

# Installs stable LaunchAgent entrypoints. It deliberately never starts, stops,
# or restarts an existing service; activation remains an explicit operator step.
set -euo pipefail
umask 077

if [[ "${1:-}" != "--install" ]]; then
  print "Preview only. This would install stable LaunchAgent entrypoints for executor, API, and web."
  print "Run with --install to write the plist files without starting or restarting services."
  exit 0
fi

script_dir="${0:A:h}"
control_center_dir="${script_dir:h:h}"
stable_root="${FLEET_STABLE_ROOT:-${HOME}/Library/Application Support/WEEXFleet}"
launch_agents_dir="${HOME}/Library/LaunchAgents"

/usr/bin/python3 - "${stable_root}" "${launch_agents_dir}" "${control_center_dir}" <<'PY'
from __future__ import annotations

import os
import plistlib
import shutil
import sys
from pathlib import Path

stable_root = Path(sys.argv[1]).expanduser()
launch_agents = Path(sys.argv[2]).expanduser()
control_center = Path(sys.argv[3]).resolve()
if "/current/" in f"{control_center}/" or control_center.name == "current":
    raise SystemExit("refusing to install a LaunchAgent from a mutable current release path")

scripts = control_center / "scripts" / "macos"
if not (scripts / "run-web.zsh").is_file():
    raise SystemExit("missing web service runner")

bin_dir = stable_root / "bin"
logs_dir = stable_root / "logs"
staging_dir = stable_root / ".launch-agent-staging"
for directory in (stable_root, bin_dir, logs_dir, staging_dir):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
launch_agents.mkdir(parents=True, exist_ok=True)

launcher = bin_dir / "launch-service.zsh"
launcher.write_text(
    "#!/bin/zsh\nset -euo pipefail\n[[ $# -ge 1 ]] || exit 64\ntarget=$1\nshift\n[[ -f \"$target\" ]] || exit 66\nexec /bin/zsh \"$target\" \"$@\"\n",
    encoding="utf-8",
)
launcher.chmod(0o700)

# macOS LaunchAgents may not have privacy access to a checkout under Documents.
# The web service only needs the published static release, so keep this runner
# and its server implementation entirely under Application Support.
static_server = bin_dir / "static_server.py"
shutil.copy2(scripts / "static_server.py", static_server)
static_server.chmod(0o600)
web_runner = bin_dir / "run-web.zsh"
web_runner.write_text(
    "#!/bin/zsh\n"
    "set -euo pipefail\n"
    "umask 077\n"
    "release_root=\"${FLEET_RELEASE_ROOT:-${HOME}/Library/Application Support/WEEXFleet}\"\n"
    "web_root=\"${FLEET_WEB_ROOT:-${release_root}/web-current}\"\n"
    "[[ -f \"${web_root}/index.html\" ]] || { print -u2 \"missing published frontend: ${web_root}/index.html\"; exit 1; }\n"
    "exec /usr/bin/python3 \"" + str(static_server) + "\" "
    "--port \"${FLEET_WEB_PORT:-37642}\" --bind 127.0.0.1 "
    "--root \"${web_root}\" --releases-root \"${release_root}/web-releases\"\n",
    encoding="utf-8",
)
web_runner.chmod(0o700)

# API and executor must never point at a mutable Documents checkout. Their
# release source and venv are selected atomically by service-current.
service_runner = """#!/bin/zsh
set -euo pipefail
umask 077
role=$1
release_root=\"${FLEET_RELEASE_ROOT:-${HOME}/Library/Application Support/WEEXFleet}\"
service_root=\"${FLEET_SERVICE_ROOT:-${release_root}/service-current}\"
env_file=\"${FLEET_ENV_FILE:-${release_root}/.env.live}\"
    [[ -f \"${service_root}/release.json\" ]] || { print -u2 \"missing service release: ${service_root}\"; exit 1; }
    service_root=\"${service_root:A}\"
    [[ -r \"${env_file}\" ]] || { print -u2 \"missing readable stable Live environment file: ${env_file}\"; exit 1; }
set -a
source \"${env_file}\"
set +a
state_root=\"${FLEET_STATE_ROOT:-${release_root}/state}\"
mkdir -p \"${state_root}\"
chmod 700 \"${state_root}\"
# Defaults must remain outside a replaceable release. An explicitly provisioned
# configuration may point elsewhere, but a relative default must never create
# a fresh account/ledger database inside the new release working directory.
export FLEET_DB_PATH=\"${FLEET_DB_PATH:-${state_root}/fleet-control.db}\"
export FLEET_CAMPAIGN_DATA_DIR=\"${FLEET_CAMPAIGN_DATA_DIR:-${state_root}/beta-campaigns-live}\"
export FLEET_EXECUTOR_SOCKET=\"${FLEET_EXECUTOR_SOCKET:-${release_root}/run/executor.sock}\"
export PYTHONPATH=\"${service_root}/src:${service_root}/control-center/server/src\"
python=\"${service_root}/control-center/server/.venv/bin/python\"
[[ -x \"${python}\" ]] || { print -u2 \"missing release Python: ${python}\"; exit 1; }
if [[ \"${role}\" == executor ]]; then
  mkdir -p \"${FLEET_EXECUTOR_SOCKET:h}\"
  export FLEET_EXECUTOR_RELEASE_ID=\"${service_root:t}\"
  exec \"${python}\" -m fleet_api.executor_main
fi
if [[ \"${role}\" == api ]]; then
  export FLEET_API_RELEASE_ID=\"${service_root:t}\"
  exec \"${python}\" -m uvicorn fleet_api.api_proxy:app --host 127.0.0.1 --port \"${FLEET_API_PORT:-37641}\"
fi
exit 64
"""
executor_runner = bin_dir / "run-executor.zsh"
executor_runner.write_text(service_runner.replace("role=$1", "role=executor"), encoding="utf-8")
executor_runner.chmod(0o700)
api_runner = bin_dir / "run-api.zsh"
api_runner.write_text(service_runner.replace("role=$1", "role=api"), encoding="utf-8")
api_runner.chmod(0o700)

targets = {
    "com.groove.weex-fleet-executor": executor_runner,
    "com.groove.weex-fleet-api": api_runner,
    "com.groove.weex-fleet-web": web_runner,
}

socket_path = stable_root / "run" / "executor.sock"
common_environment = {
    "FLEET_EXECUTOR_SOCKET": str(socket_path),
    "FLEET_RELEASE_ROOT": str(stable_root),
}
service_environments = {
    "com.groove.weex-fleet-executor": common_environment,
    "com.groove.weex-fleet-api": common_environment,
    "com.groove.weex-fleet-web": {
        **common_environment,
        "FLEET_WEB_ROOT": str(stable_root / "web-current"),
    },
}

for label, target in targets.items():
    plist = {
        "Label": label,
        "ProgramArguments": ["/bin/zsh", str(launcher), str(target)],
        "WorkingDirectory": str(stable_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "EnvironmentVariables": service_environments[label],
        "StandardOutPath": str(logs_dir / f"{label}.out.log"),
        "StandardErrorPath": str(logs_dir / f"{label}.err.log"),
    }
    staged = staging_dir / f"{label}.plist"
    with staged.open("wb") as stream:
        plistlib.dump(plist, stream, sort_keys=False)
    staged.chmod(0o600)
    destination = launch_agents / staged.name
    shutil.copy2(staged, destination)
    destination.chmod(0o600)
    print(f"installed {destination}")

for path in staging_dir.iterdir():
    path.unlink()
staging_dir.rmdir()
PY

print "LaunchAgent files were installed but not activated. Existing API, web, and executor processes were not touched."

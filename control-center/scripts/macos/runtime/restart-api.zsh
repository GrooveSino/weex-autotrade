#!/bin/zsh

# Explicit API-only restart. It never starts or restarts the executor.
set -euo pipefail

if [[ "${1:-}" != "--restart" ]]; then
  print "Refusing to restart the API without --restart. This command never restarts the executor."
  exit 64
fi

script_dir="${0:A:h}"
control_center_dir="${script_dir:h:h:h}"
socket_path="${FLEET_EXECUTOR_SOCKET:-${HOME}/Library/Application Support/WEEXFleet/run/executor.sock}"

"${control_center_dir}/server/.venv/bin/python3" - "${socket_path}" <<'PY'
from __future__ import annotations

import sys

import httpx

socket = sys.argv[1]
try:
    with httpx.Client(transport=httpx.HTTPTransport(uds=socket), base_url="http://fleet-executor", timeout=3) as client:
        response = client.get("/_internal/executor-health")
        response.raise_for_status()
        payload = response.json()
except httpx.HTTPError as exc:
    raise SystemExit(f"executor health precheck failed: {exc}") from exc
if not payload.get("executorConnected", True) or not payload.get("executorGeneration"):
    raise SystemExit("executor health precheck did not return a generation")
PY

launchctl kickstart -k "gui/${UID}/com.groove.weex-fleet-api"
print "API restart requested after executor health precheck. Executor was not restarted."

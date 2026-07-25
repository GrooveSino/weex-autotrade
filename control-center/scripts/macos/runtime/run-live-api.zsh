#!/bin/zsh

set -euo pipefail
umask 077

script_dir="${0:A:h}"
control_center_dir="${script_dir:h:h:h}"
env_file="${control_center_dir}/.env.live"

if [[ ! -r "${env_file}" ]]; then
  print -u2 "missing readable Live environment file: ${env_file}"
  exit 1
fi

set -a
source "${env_file}"
set +a

api_port="${FLEET_API_PORT:-37641}"
export FLEET_EXECUTOR_SOCKET="${FLEET_EXECUTOR_SOCKET:-${HOME}/Library/Application Support/WEEXFleet/run/executor.sock}"
cd "${control_center_dir}"

exec "${control_center_dir}/server/.venv/bin/uvicorn" \
  --app-dir "${control_center_dir}/server/src" \
  fleet_api.api_proxy:app \
  --host 127.0.0.1 \
  --port "${api_port}"

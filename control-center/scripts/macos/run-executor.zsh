#!/bin/zsh

set -euo pipefail
umask 077

script_dir="${0:A:h}"
control_center_dir="${script_dir:h:h}"
env_file="${control_center_dir}/.env.live"

if [[ ! -r "${env_file}" ]]; then
  print -u2 "missing readable Live environment file: ${env_file}"
  exit 1
fi

set -a
source "${env_file}"
set +a

export FLEET_EXECUTOR_SOCKET="${FLEET_EXECUTOR_SOCKET:-${HOME}/Library/Application Support/WEEXFleet/run/executor.sock}"
mkdir -p "${FLEET_EXECUTOR_SOCKET:h}"
cd "${control_center_dir}"

exec "${control_center_dir}/server/.venv/bin/python3" \
  -m fleet_api.executor_main

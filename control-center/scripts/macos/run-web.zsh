#!/bin/zsh

set -euo pipefail
umask 077

script_dir="${0:A:h}"
control_center_dir="${script_dir:h:h}"
dist_dir="${FLEET_WEB_ROOT:-${control_center_dir}/dist}"
release_root="${FLEET_RELEASE_ROOT:-${HOME}/Library/Application Support/WEEXFleet}"

if [[ ! -f "${dist_dir}/index.html" ]]; then
  print -u2 "missing production build: ${dist_dir}/index.html"
  exit 1
fi

web_port="${FLEET_WEB_PORT:-37642}"

exec "${control_center_dir}/server/.venv/bin/python3" \
  "${script_dir}/static_server.py" \
  --port "${web_port}" \
  --bind 127.0.0.1 \
  --root "${dist_dir}" \
  --releases-root "${release_root}/web-releases"

#!/bin/zsh

# Installs the Mac mini -> VPS reverse tunnel without activating it. The tunnel
# listens only on the VPS loopback interface, so Caddy is its only public path.
set -euo pipefail
umask 077

if [[ "${1:-}" != "--install" ]]; then
  print "Preview only. This would install the stable VPS reverse-tunnel LaunchAgent."
  print "Run with --install to write it; bootstrap remains an explicit deployment step."
  exit 0
fi

stable_root="${FLEET_STABLE_ROOT:-${HOME}/Library/Application Support/WEEXFleet}"
launch_agents_dir="${HOME}/Library/LaunchAgents"
label="com.groove.weex-fleet-vps-tunnel"
runner="${stable_root}/bin/run-vps-tunnel.zsh"
plist="${launch_agents_dir}/${label}.plist"

mkdir -p "${stable_root}/bin" "${stable_root}/logs" "${launch_agents_dir}"
chmod 700 "${stable_root}" "${stable_root}/bin" "${stable_root}/logs"

cat > "${runner}" <<'RUNNER'
#!/bin/zsh
set -euo pipefail
umask 077

api_port="${FLEET_API_PORT:-37641}"
web_port="${FLEET_WEB_PORT:-37642}"
tunnel_target="${FLEET_VPS_TUNNEL_TARGET:-weex-cloudserver}"

# Do not create a public path to a partially started executor/API pair.
for _ in {1..90}; do
  if curl --silent --fail --max-time 2 "http://127.0.0.1:${api_port}/api/v1/health" >/dev/null \
    && curl --silent --fail --max-time 2 "http://127.0.0.1:${web_port}/__fleet/version.json" >/dev/null; then
    break
  fi
  sleep 2
done

curl --silent --fail --max-time 2 "http://127.0.0.1:${api_port}/api/v1/health" >/dev/null
curl --silent --fail --max-time 2 "http://127.0.0.1:${web_port}/__fleet/version.json" >/dev/null

exec /usr/bin/ssh -N \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=20 \
  -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=yes \
  -R 127.0.0.1:39461:127.0.0.1:${api_port} \
  -R 127.0.0.1:39462:127.0.0.1:${web_port} \
  "${tunnel_target}"
RUNNER
chmod 700 "${runner}"

/usr/bin/python3 - "${plist}" "${label}" "${runner}" "${stable_root}" <<'PY'
from __future__ import annotations

import plistlib
import sys
from pathlib import Path

plist_path = Path(sys.argv[1])
label = sys.argv[2]
runner = sys.argv[3]
stable_root = Path(sys.argv[4])

payload = {
    "Label": label,
    "ProgramArguments": [runner],
    "RunAtLoad": False,
    "KeepAlive": True,
    "ThrottleInterval": 5,
    "ProcessType": "Background",
    "StandardOutPath": str(stable_root / "logs" / f"{label}.out.log"),
    "StandardErrorPath": str(stable_root / "logs" / f"{label}.err.log"),
}
with plist_path.open("wb") as handle:
    plistlib.dump(payload, handle, sort_keys=False)
plist_path.chmod(0o600)
PY

print "Installed ${plist}; it has not been started."

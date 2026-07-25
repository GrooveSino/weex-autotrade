#!/bin/zsh

# Stage the current checkout as a stable local release and activate it. The
# explicitly supplied environment file is copied to Application Support with
# owner-only permissions; no secret is written into a release directory.
set -euo pipefail
umask 077

script_name="${0:t}"

usage() {
  print "Usage: ${script_name} --env /absolute/path/to/.env.live --apply"
  print "Stages API/executor and web releases, installs stable LaunchAgents, then activates them."
}

env_file=""
apply=false
while (( $# > 0 )); do
  case "$1" in
    --env)
      (( $# >= 2 )) || { usage >&2; exit 64; }
      env_file="$2"
      shift 2
      ;;
    --apply)
      apply=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      print -u2 "unknown argument: $1"
      usage >&2
      exit 64
      ;;
  esac
done

[[ -n "${env_file}" ]] || { print -u2 "--env is required"; usage >&2; exit 64; }
[[ -r "${env_file}" ]] || { print -u2 "cannot read environment file: ${env_file}"; exit 66; }
env_file="${env_file:A}"

if ! ${apply}; then
  print "Preview only. No release, LaunchAgent, or environment file will be changed."
  print "Would validate and copy: ${env_file}"
  print "Then run again with --apply."
  exit 0
fi

set -a
source "${env_file}"
set +a

[[ "${FLEET_CONTROL_ADAPTER:-}" == "weex-live" ]] || {
  print -u2 "FLEET_CONTROL_ADAPTER must be weex-live in the explicitly supplied environment file"
  exit 65
}
[[ "${FLEET_STORAGE:-}" == "sqlite" ]] || { print -u2 "FLEET_STORAGE must be sqlite"; exit 65; }
[[ -n "${FLEET_MASTER_KEY:-}" ]] || { print -u2 "FLEET_MASTER_KEY is required"; exit 65; }
[[ -n "${FLEET_BETA_RATIO_URL:-}" ]] || { print -u2 "FLEET_BETA_RATIO_URL is required"; exit 65; }
[[ "${FLEET_LIVE_CAMPAIGNS_ENABLED:-}" == "true" ]] || { print -u2 "FLEET_LIVE_CAMPAIGNS_ENABLED=true is required"; exit 65; }
[[ "${WEEX_LIVE_TRADING_ENABLED:-}" == "true" ]] || { print -u2 "WEEX_LIVE_TRADING_ENABLED=true is required"; exit 65; }

# Shared public depth has exactly one direct egress by default.  Reject a
# release before any service switch when that route cannot reach WEEX: a
# healthy-looking API with no shared market would otherwise hold every normal
# Maker phase in the recovery queue.
if [[ "${FLEET_LIVE_CAMPAIGN_WEBSOCKETS_ENABLED:-false}" == "true" \
  && -z "${FLEET_SHARED_MARKET_DATA_PROXY_URL:-}" ]]; then
  if ! nc -G 5 -z ws-contract.weex.com 443 >/dev/null 2>&1; then
    print -u2 "refusing deployment: Mac mini cannot directly reach WEEX public market data (ws-contract.weex.com:443)"
    print -u2 "restore direct egress or configure a dedicated non-account shared market proxy before retrying"
    exit 70
  fi
fi

script_dir="${0:A:h}"
control_center_dir="${script_dir:h:h:h}"
release_root="${FLEET_RELEASE_ROOT:-${HOME}/Library/Application Support/WEEXFleet}"
stable_env="${release_root}/.env.live"
users_toml="${release_root}/users.toml"
api_health="http://127.0.0.1:${FLEET_API_PORT:-37641}/api/v1/health"

# A service release restarts the executor. Do not convert a running task into
# recovery state merely because the operator is publishing UI or code.
if health="$(curl --silent --show-error --fail --max-time 1 "${api_health}" 2>/dev/null)"; then
  active_workers="$(python3 -c 'import json,sys; print(int(json.load(sys.stdin).get("liveCampaignActiveWorkerCount", 0)))' <<<"${health}")"
  if (( active_workers > 0 )); then
    print -u2 "refusing deployment while ${active_workers} Live worker(s) are active"
    exit 69
  fi
fi

mkdir -p "${release_root}"
chmod 700 "${release_root}"
# Operators may deliberately deploy from the existing stable environment file.
# GNU/BSD install rejects a source and destination that resolve to the same
# file, so leave that file in place instead of making a needless copy.
if [[ "${env_file:A}" != "${stable_env:A}" ]]; then
  install -m 600 "${env_file}" "${stable_env}"
else
  chmod 600 "${stable_env}"
fi

# Local console users are deliberately independent from exchange credentials.
# First deployment creates gg and colin with random 32-character passwords in
# an owner-only file. The provisioning helper emits no generated password.
if [[ ! -e "${users_toml}" ]]; then
  UV_CACHE_DIR="${UV_CACHE_DIR:-/private/tmp/weex-uv-cache}" \
    uv run --project "${control_center_dir}/server" python \
    "${control_center_dir}/server/scripts/provision_local_users.py" --path "${users_toml}"
fi
chmod 600 "${users_toml}"

# Keep the stable environment idempotent across releases. These operations
# never print the environment file or the generated user registry.
if /usr/bin/grep -q '^FLEET_LOCAL_USER_AUTH_REQUIRED=' "${stable_env}"; then
  /usr/bin/sed -i '' 's/^FLEET_LOCAL_USER_AUTH_REQUIRED=.*/FLEET_LOCAL_USER_AUTH_REQUIRED=true/' "${stable_env}"
else
  print 'FLEET_LOCAL_USER_AUTH_REQUIRED=true' >> "${stable_env}"
fi
if /usr/bin/grep -q '^FLEET_USERS_TOML=' "${stable_env}"; then
  /usr/bin/sed -i '' "s|^FLEET_USERS_TOML=.*|FLEET_USERS_TOML='${users_toml}'|" "${stable_env}"
else
  print "FLEET_USERS_TOML='${users_toml}'" >> "${stable_env}"
fi
chmod 600 "${stable_env}"

# The public Caddy deployment serves this console at /fleet/ while the API is
# mounted below /fleet/api/v1. Existing operators may override either value in
# their explicitly provisioned environment file.
if ! /usr/bin/grep -q '^FLEET_WEB_PUBLIC_BASE_PATH=' "${stable_env}"; then
  print 'FLEET_WEB_PUBLIC_BASE_PATH=/fleet/' >> "${stable_env}"
  export FLEET_WEB_PUBLIC_BASE_PATH='/fleet/'
fi
if ! /usr/bin/grep -q '^FLEET_WEB_API_BASE_URL=' "${stable_env}"; then
  print 'FLEET_WEB_API_BASE_URL=/fleet/api/v1' >> "${stable_env}"
  export FLEET_WEB_API_BASE_URL='/fleet/api/v1'
fi

release_dir="${script_dir}"
runtime_dir="${control_center_dir}/scripts/macos/runtime"
FLEET_ENV_FILE="${stable_env}" "${release_dir}/deploy-service-release.zsh"
FLEET_ENV_FILE="${stable_env}" "${release_dir}/deploy-web-release.zsh"
FLEET_STABLE_ROOT="${release_root}" "${runtime_dir}/install-launch-agents.zsh" --install
FLEET_ENV_FILE="${stable_env}" "${release_dir}/activate-service-release.zsh"

domain="gui/${UID}"
web_label="com.groove.weex-fleet-web"
web_plist="${HOME}/Library/LaunchAgents/${web_label}.plist"
launchctl bootout "${domain}/${web_label}" 2>/dev/null || true
launchctl bootstrap "${domain}" "${web_plist}"
for _ in {1..50}; do
  if curl --silent --fail --max-time 1 "http://127.0.0.1:${FLEET_WEB_PORT:-37642}/__fleet/version.json" >/dev/null; then
    print "local WEEX Fleet release is active"
    exit 0
  fi
  sleep 0.1
done
print -u2 "web service did not pass its local health check"
exit 1

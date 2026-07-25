#!/bin/zsh

# Activate an already-staged service release. This never creates or resumes a
# trading task; a restarted executor recovers incomplete work as uncertain and
# enters read-only recovery before any further Live command.
set -euo pipefail
umask 077

release_root="${FLEET_RELEASE_ROOT:-${HOME}/Library/Application Support/WEEXFleet}"
env_file="${FLEET_ENV_FILE:-${release_root}/.env.live}"
socket_path="${FLEET_EXECUTOR_SOCKET:-${release_root}/run/executor.sock}"
legacy_api_health_url="${FLEET_LEGACY_API_HEALTH_URL:-http://127.0.0.1:37641/api/v1/health}"
launch_agents_dir="${HOME}/Library/LaunchAgents"
domain="gui/${UID}"
executor_label="com.groove.weex-fleet-executor"
api_label="com.groove.weex-fleet-api"
executor_plist="${launch_agents_dir}/${executor_label}.plist"
api_plist="${launch_agents_dir}/${api_label}.plist"
previous_link="${release_root}/service-previous"

[[ -f "${release_root}/service-current/release.json" ]] || {
  print -u2 "missing staged service release: ${release_root}/service-current"
  exit 1
}
[[ -r "${env_file}" ]] || {
  print -u2 "missing stable Live environment file: ${env_file}"
  exit 1
}
set -a
source "${env_file}"
set +a
[[ -f "${executor_plist}" && -f "${api_plist}" ]] || {
  print -u2 "stable LaunchAgent files are not installed"
  exit 1
}

# launchctl bootout returns before launchd has always finished removing the
# job. Waiting for the label to disappear prevents an immediate bootstrap
# from intermittently failing with an opaque Input/output error on macOS.
wait_for_unload() {
  local label="$1"
  for _ in {1..50}; do
    if ! launchctl print "${domain}/${label}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.1
  done
  print -u2 "LaunchAgent did not finish unloading: ${label}"
  return 1
}

# A legacy in-process API can own workers and the same persistent account
# state. Do not bring up a second executor until its local health projection
# proves that no worker is active. This is a local API-only check and never
# contacts WEEX.
legacy_health="$(curl --silent --show-error --fail --max-time 3 "${legacy_api_health_url}" 2>/dev/null || true)"
if [[ -n "${legacy_health}" ]]; then
legacy_state="$(FLEET_LEGACY_HEALTH="${legacy_health}" FLEET_LEGACY_API_HEALTH_URL="${legacy_api_health_url}" /usr/bin/python3 - <<'PY'
import json
import os
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import urlopen

try:
    health = json.loads(os.environ["FLEET_LEGACY_HEALTH"])
except (KeyError, json.JSONDecodeError):
    print("unverifiable")
else:
    if health.get("executorConnected") is True:
        print("proxy")
    else:
        active = health.get("liveCampaignActiveWorkerCount")
        if isinstance(active, int) and not isinstance(active, bool):
            print("clear" if active == 0 else "active")
        else:
            # Legacy releases did not expose the actual worker count. Their
            # campaign lists are local journal reads, so inspect them before
            # allowing an executor migration. Do not use configured capacity.
            try:
                parsed = urlsplit(os.environ["FLEET_LEGACY_API_HEALTH_URL"])
                base = urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")
                with urlopen(f"{base}/api/v1/instances", timeout=1) as response:
                    instances = json.load(response)
                if not isinstance(instances, list):
                    raise ValueError("instances response is not a list")
                for instance in instances:
                    if not isinstance(instance, dict) or instance.get("mode") != "live":
                        continue
                    instance_id = instance.get("id")
                    if not isinstance(instance_id, str) or not instance_id:
                        raise ValueError("live instance id is invalid")
                    with urlopen(
                        f"{base}/api/v1/instances/{quote(instance_id, safe='')}/beta-campaigns",
                        timeout=1,
                    ) as response:
                        campaigns = json.load(response)
                    if not isinstance(campaigns, list):
                        raise ValueError("campaign response is not a list")
                    if any(isinstance(item, dict) and item.get("status") in {"executing", "stopping"} for item in campaigns):
                        print("active")
                        break
                else:
                    print("clear")
            except Exception:
                print("unverifiable")
PY
)"
  case "${legacy_state}" in
    proxy|clear) ;;
    active)
      print -u2 "legacy API still owns active Live campaign workers; safely stop or reconcile them before executor migration"
      exit 1
      ;;
    *)
      print -u2 "legacy API health cannot prove it has no Live campaign workers; refusing executor migration"
      exit 1
      ;;
  esac
elif [[ -S "${socket_path}" ]]; then
  # A wedged proxy must not hide the executor's local safety state. The private
  # owner-only socket exposes the same worker count without contacting WEEX.
  direct_health="$(curl --silent --show-error --fail --max-time 3 \
    --unix-socket "${socket_path}" "http://localhost/_internal/executor-health" 2>/dev/null || true)"
  direct_state="$(FLEET_DIRECT_HEALTH="${direct_health}" /usr/bin/python3 - <<'PY'
import json
import os

try:
    health = json.loads(os.environ["FLEET_DIRECT_HEALTH"])
except (KeyError, json.JSONDecodeError):
    print("unverifiable")
else:
    active = health.get("liveCampaignActiveWorkerCount")
    print("clear" if active == 0 else "active" if isinstance(active, int) else "unverifiable")
PY
)"
  case "${direct_state}" in
    clear) ;;
    active)
      print -u2 "executor still owns active Live campaign workers; refusing service activation"
      exit 1
      ;;
    *)
      # If the old event loop is wedged, require two independent persistent
      # signals before replacement: no executing/stopping journal row and no
      # account lease held by a worker process.
      persisted_state="$(/usr/bin/python3 - <<'PY'
import fcntl
import os
import sqlite3
from pathlib import Path

database = Path(os.environ.get("FLEET_DB_PATH", "")).expanduser()
lock_root = Path(os.environ.get("FLEET_CAMPAIGN_DATA_DIR", "")).expanduser() / "locks"
if not database.is_file():
    print("unverifiable")
    raise SystemExit
try:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=3)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "beta_campaigns" not in tables:
        print("unverifiable")
        raise SystemExit
    active = connection.execute(
        "SELECT COUNT(*) FROM beta_campaigns WHERE status IN ('executing', 'stopping')"
    ).fetchone()[0]
finally:
    if "connection" in locals():
        connection.close()
if active:
    print("active")
    raise SystemExit
for path in lock_root.glob("account-*.lock"):
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("active")
            raise SystemExit
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
print("clear")
PY
)"
      if [[ "${persisted_state}" != clear ]]; then
        print -u2 "API, executor, and persistent state cannot prove there are no active workers; refusing activation"
        exit 1
      fi
      ;;
  esac
elif /usr/sbin/lsof -nP -iTCP:37641 -sTCP:LISTEN 2>/dev/null | /usr/bin/grep -q .; then
  print -u2 "an API listener is present but neither API nor executor health can be verified; refusing migration"
  exit 1
fi

# Keep the legacy API reachable until the executor answers through its private
# socket. No external exchange endpoint is contacted by either health check.
launchctl bootout "${domain}/${executor_label}" 2>/dev/null || true
wait_for_unload "${executor_label}"
launchctl enable "${domain}/${executor_label}"
launchctl bootstrap "${domain}" "${executor_plist}"
# The executor opens its socket only after loading the encrypted account store
# and persisted campaign/volume state. A cold SQLite start can exceed five
# seconds on macOS, so allow a bounded local-only readiness window before
# touching the API. This does not contact WEEX or resume any Campaign.
executor_ready_checks=0
for _ in {1..200}; do
  if [[ -S "${socket_path}" ]] && curl --silent --fail --max-time 3 \
    --unix-socket "${socket_path}" "http://localhost/_internal/executor-health" >/dev/null; then
    executor_ready_checks=$((executor_ready_checks + 1))
    if (( executor_ready_checks >= 2 )); then
      break
    fi
  else
    executor_ready_checks=0
  fi
  sleep 0.1
done
(( executor_ready_checks >= 2 )) || {
  print -u2 "executor did not pass its local health check"
  exit 1
}

launchctl bootout "${domain}/${api_label}" 2>/dev/null || true
wait_for_unload "${api_label}"
launchctl enable "${domain}/${api_label}"
launchctl bootstrap "${domain}" "${api_plist}"
for _ in {1..50}; do
  if curl --silent --fail --max-time 1 http://127.0.0.1:37641/api/v1/health >/dev/null; then
    print "service release activated: $(readlink "${release_root}/service-current")"
    exit 0
  fi
  sleep 0.1
done
if [[ -L "${previous_link}" && -f "${previous_link:A}/release.json" ]]; then
  previous_release="${previous_link:A}"
  ln -sfn "${previous_release}" "${release_root}/service-current.rollback"
  mv -f -h "${release_root}/service-current.rollback" "${release_root}/service-current"
  launchctl bootout "${domain}/${api_label}" 2>/dev/null || true
  wait_for_unload "${api_label}"
  launchctl enable "${domain}/${api_label}"
  launchctl bootstrap "${domain}" "${api_plist}"
  for _ in {1..50}; do
    if curl --silent --fail --max-time 1 http://127.0.0.1:37641/api/v1/health >/dev/null; then
      print -u2 "new API release failed health; restored previous API release: ${previous_release}"
      exit 1
    fi
    sleep 0.1
  done
fi
print -u2 "API did not pass health after executor activation"
exit 1

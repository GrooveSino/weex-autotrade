#!/bin/zsh

# Build a stable source/runtime release for the API proxy and executor. This
# script does not submit orders and does not restart either service; activation
# happens only after its release has passed the read-only health checks.
set -euo pipefail
umask 077

script_dir="${0:A:h}"
control_center_dir="${script_dir:h:h:h}"
repo_root="${control_center_dir:h}"
release_root="${FLEET_RELEASE_ROOT:-${HOME}/Library/Application Support/WEEXFleet}"
releases_root="${release_root}/service-releases"
current_link="${release_root}/service-current"
previous_link="${release_root}/service-previous"
release_id="$(date -u +%Y%m%dT%H%M%SZ)-$(git -C "${repo_root}" rev-parse --short HEAD 2>/dev/null || print local)-$$-${RANDOM}"
stage_dir="${releases_root}/.${release_id}.staging"
release_dir="${releases_root}/${release_id}"

mkdir -p "${releases_root}"
[[ ! -e "${release_dir}" ]] || { print -u2 "release already exists: ${release_id}"; exit 1; }
cleanup() { [[ -e "${stage_dir}" ]] && rm -rf "${stage_dir}"; }
trap cleanup EXIT

mkdir -p "${stage_dir}/control-center/server"

# Build the tracked baseline from Git objects when available. A source archive
# copied to a new Mac may deliberately omit .git, so fall back to copying only
# the two reviewed source roots in that case. Neither path includes credentials
# or mutable runtime state.
if git -C "${repo_root}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  # Reading every unchanged file from a macOS file-provider workspace can block
  # on hydration for minutes.
  git -C "${repo_root}" archive HEAD src control-center/server/src | tar -x -C "${stage_dir}"

  # A release must include the current dirty worktree as well as HEAD. Overlay
  # only changed and untracked source files, and remove tracked deletions from
  # the archive baseline. Credentials and runtime artifacts are outside these
  # roots.
  while IFS= read -r -d '' relative_path; do
    source_path="${repo_root}/${relative_path}"
    target_path="${stage_dir}/${relative_path}"
    if [[ -f "${source_path}" ]]; then
      mkdir -p "${target_path:h}"
      cp -p "${source_path}" "${target_path}"
    elif [[ -e "${target_path}" ]]; then
      find "${target_path}" -depth -delete
    fi
  done < <(
    git -C "${repo_root}" ls-files -z --modified --deleted --others --exclude-standard -- \
      src control-center/server/src
  )
else
  rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
    "${repo_root}/src/" "${stage_dir}/src/"
  rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
    "${control_center_dir}/server/src/" "${stage_dir}/control-center/server/src/"
fi
# Bytecode caches are runtime-generated and can contain macOS metadata that
# makes a release copy needlessly slow. The project package in site-packages is
# also redundant: the stable runner imports the immutable release sources via
# PYTHONPATH. Excluding both avoids hydrating stale file-provider duplicates.
if [[ -L "${current_link}" ]]; then
  previous_release="${current_link:A}"
fi
previous_venv="${previous_release:-}/control-center/server/.venv"
if [[ -n "${previous_release:-}" && -x "${previous_venv}/bin/python" ]]; then
  # APFS clone-copy keeps the release independent while avoiding thousands of
  # file-provider reads from the workspace. Runtime imports project code from
  # this release's PYTHONPATH, not from the copied site-packages package.
  cp -cR "${previous_venv}" "${stage_dir}/control-center/server/.venv"
else
  rsync -a --delete \
    --exclude '__pycache__' --exclude '*.pyc' \
    --exclude 'lib/python*/site-packages/weex_cli' \
    --exclude 'lib/python*/site-packages/weex_autotrade-*.dist-info' \
    "${control_center_dir}/server/.venv/" "${stage_dir}/control-center/server/.venv/"
fi
cp "${repo_root}/pyproject.toml" "${stage_dir}/pyproject.toml"
cp "${control_center_dir}/server/pyproject.toml" "${stage_dir}/control-center/server/pyproject.toml" 2>/dev/null || true

"${stage_dir}/control-center/server/.venv/bin/python" - "${stage_dir}" "${release_id}" <<'PY'
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

release = Path(sys.argv[1])
release_id = sys.argv[2]
required = [
    release / "src/weex_cli/beta_campaign/__init__.py",
    release / "control-center/server/src/fleet_api/executor_main.py",
    release / "control-center/server/src/fleet_api/api_proxy.py",
    release / "control-center/server/.venv/bin/python",
]
missing = [str(path.relative_to(release)) for path in required if not path.is_file()]
if missing:
    raise SystemExit(f"service release missing: {', '.join(missing)}")
(release / "release.json").write_text(
    json.dumps(
        {
            "release_id": release_id,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "api_compatibility": "v1",
            "contains_credentials": False,
        },
        separators=(",", ":"),
    ),
    encoding="utf-8",
)
PY

PYTHONPATH="${stage_dir}/src:${stage_dir}/control-center/server/src" \
  "${stage_dir}/control-center/server/.venv/bin/python" - <<'PY'
from fleet_api.api_proxy import create_app as create_proxy_app
from fleet_api.executor_main import create_app as create_executor_app
from weex_cli.control_api.progress import ExecutionProgressProjector

assert create_proxy_app is not None
assert create_executor_app is not None
assert ExecutionProgressProjector is not None
PY

mv "${stage_dir}" "${release_dir}"
if [[ -n "${previous_release:-}" ]]; then
  ln -sfn "${previous_release}" "${previous_link}.next"
  mv -f -h "${previous_link}.next" "${previous_link}"
fi
ln -sfn "${release_dir}" "${current_link}.next"
mv -f -h "${current_link}.next" "${current_link}"
print "service release staged and active for the next explicit API/executor activation: ${release_id}"

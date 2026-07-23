#!/bin/zsh

set -euo pipefail
umask 077

script_dir="${0:A:h}"
control_center_dir="${script_dir:h:h}"
release_root="${FLEET_RELEASE_ROOT:-${HOME}/Library/Application Support/WEEXFleet}"
web_releases="${release_root}/web-releases"
current_link="${release_root}/web-current"
release_id="$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import time; print(time.time_ns())')-$$-${RANDOM}-$(git -C "${control_center_dir:h}" rev-parse --short HEAD 2>/dev/null || print local)"
release_dir="${web_releases}/${release_id}"
stage_dir="${web_releases}/.${release_id}.staging"
python_bin="${FLEET_PYTHON_BIN:-${control_center_dir}/server/.venv/bin/python3}"

if [[ ! -x "${python_bin}" ]]; then
  stable_python="${release_root}/service-current/control-center/server/.venv/bin/python3"
  [[ -x "${stable_python}" ]] || {
    print -u2 "Fleet Python runtime was not found; stage a service release first or set FLEET_PYTHON_BIN"
    exit 69
  }
  python_bin="${stable_python}"
fi

mkdir -p "${web_releases}"
[[ ! -e "${release_dir}" ]] || { print -u2 "release already exists: ${release_id}"; exit 1; }
cleanup_stage() {
  [[ -e "${stage_dir}" ]] && rm -rf "${stage_dir}"
}
trap cleanup_stage EXIT

cd "${control_center_dir}"
build_source="${FLEET_WEB_DIST_SOURCE:-${control_center_dir}/dist}"
if [[ -z "${FLEET_WEB_DIST_SOURCE:-}" ]]; then
  # The public Fleet console is mounted below a reverse-proxy prefix. Keep
  # this distinct from the loopback static server root so Vite emits asset and
  # API URLs that work on the public route.
  web_public_base_path="${FLEET_WEB_PUBLIC_BASE_PATH:-${VITE_PUBLIC_BASE_PATH:-/}}"
  web_api_base_url="${FLEET_WEB_API_BASE_URL:-${VITE_API_BASE_URL:-/api/v1}}"
  # LaunchAgent and remote SSH shells do not load interactive fnm/nvm setup.
  # Resolve npm explicitly so a normal release does not depend on shell rc files.
  npm_bin="${FLEET_NPM_BIN:-}"
  if [[ -z "${npm_bin}" ]] && (( $+commands[npm] )); then
    npm_bin="${commands[npm]}"
  fi
  if [[ -z "${npm_bin}" ]]; then
    for candidate in \
      /opt/homebrew/bin/npm \
      /usr/local/bin/npm \
      "${HOME}"/.local/share/fnm/node-versions/*/installation/bin/npm(N); do
      if [[ -x "${candidate}" ]]; then
        # Keep the npm symlink path. Resolving it points into npm's lib tree,
        # whose parent does not contain the matching node executable.
        npm_bin="${candidate}"
      fi
    done
  fi
  [[ -n "${npm_bin}" && -x "${npm_bin}" ]] || {
    print -u2 "npm was not found; set FLEET_NPM_BIN to an executable npm path"
    exit 69
  }
  export PATH="${npm_bin:h}:${PATH}"
  VITE_PUBLIC_BASE_PATH="${web_public_base_path}" \
    VITE_API_BASE_URL="${web_api_base_url}" \
    "${npm_bin}" run build
fi
[[ -f "${build_source}/index.html" ]] || { print -u2 "missing built frontend: ${build_source}/index.html"; exit 1; }
mkdir "${stage_dir}"
cp -R "${build_source}"/. "${stage_dir}/"

"${python_bin}" - "${stage_dir}" "${release_id}" <<'PY'
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

release = Path(sys.argv[1])
release_id = sys.argv[2]
index_path = release / "index.html"
if not index_path.is_file():
    raise SystemExit("release is missing index.html")
index = index_path.read_text(encoding="utf-8")
assets_directory = release / "assets"
assets = sorted(path.relative_to(release).as_posix() for path in assets_directory.rglob("*") if path.is_file())
if not assets:
    raise SystemExit("release is missing Vite assets")
for token in ("href=\"", "src=\""):
    start = 0
    while True:
        start = index.find(token, start)
        if start < 0:
            break
        start += len(token)
        end = index.find("\"", start)
        if end < 0:
            raise SystemExit("malformed asset reference in index.html")
        reference = index[start:end]
        if reference.startswith("/assets/"):
            asset = release / reference.lstrip("/")
            if not asset.is_file():
                raise SystemExit(f"missing referenced Vite asset: {reference}")
        start = end + 1
(release / "release.json").write_text(
    json.dumps(
        {
            "release_id": release_id,
            "built_at": datetime.now(UTC).isoformat(),
            "api_compatibility": "v1",
            "assets": assets,
            "asset_count": len(assets),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ),
    encoding="utf-8",
)
PY

mv "${stage_dir}" "${release_dir}"
ln -sfn "${release_dir}" "${current_link}.next"
mv -f -h "${current_link}.next" "${current_link}"

"${python_bin}" - "${web_releases}" "${current_link}" <<'PY'
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

releases = Path(sys.argv[1])
current = Path(sys.argv[2]).resolve()
cutoff = time.time() - 7 * 24 * 60 * 60
items = sorted(
    (path for path in releases.iterdir() if path.is_dir() and not path.name.startswith(".")),
    key=lambda path: path.stat().st_mtime,
    reverse=True,
)
for index, path in enumerate(items):
    if path == current or index < 5 or path.stat().st_mtime >= cutoff:
        continue
    shutil.rmtree(path)
PY

print "web release active: ${release_id}"

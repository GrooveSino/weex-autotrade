#!/bin/zsh

set -euo pipefail
umask 077

release_root="${FLEET_RELEASE_ROOT:-${HOME}/Library/Application Support/WEEXFleet}"
web_releases="${release_root}/web-releases"
current_link="${release_root}/web-current"
target="${1:-}"

if [[ -z "${target}" ]]; then
  target="$(python3 - "${web_releases}" "${current_link}" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

releases = Path(sys.argv[1])
current = Path(sys.argv[2]).resolve(strict=False)
items = sorted(
    (path for path in releases.iterdir() if path.is_dir() and not path.name.startswith(".") and (path / "index.html").is_file() and (path / "release.json").is_file()),
    key=lambda path: path.stat().st_mtime,
    reverse=True,
)
for item in items:
    if item.resolve() != current:
        print(item)
        break
else:
    raise SystemExit("no verified previous web release available")
PY
)"
else
  [[ "${target}" == "${target:t}" ]] || { print -u2 "release id must not contain a path"; exit 1; }
  target="${web_releases}/${target}"
fi
[[ -f "${target}/index.html" && -f "${target}/release.json" ]] || { print -u2 "invalid web release: ${target}"; exit 1; }
ln -sfn "${target}" "${current_link}.next"
mv -f -h "${current_link}.next" "${current_link}"
print "web release rollback active: ${target:t}"

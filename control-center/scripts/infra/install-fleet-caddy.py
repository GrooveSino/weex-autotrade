#!/usr/bin/env python3
"""Install the narrowly scoped WEEX Fleet proxy routes into an existing Caddyfile.

This tool intentionally refuses to guess the generic fallback location. It
creates a timestamped sibling backup, validates the new configuration, and
restores that backup if validation fails.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: install-fleet-caddy.py /etc/caddy/Caddyfile")

    caddyfile = Path(sys.argv[1])
    content = caddyfile.read_text(encoding="utf-8")
    managed_marker = "redir /fleet /fleet/ 308"
    if managed_marker in content:
        required = (
            "@fleet_api path /fleet/api/*",
            "uri strip_prefix /fleet",
            "reverse_proxy 127.0.0.1:39461",
            "handle_path /fleet/*",
            "reverse_proxy 127.0.0.1:39462",
        )
        if all(item in content for item in required):
            print("WEEX Fleet routes are already installed")
            return 0
        raise SystemExit("an incomplete /fleet route already exists; refusing to modify it")

    fallback_marker = "try_files {path}"
    if content.count(fallback_marker) != 1:
        raise SystemExit("expected exactly one generic try_files fallback; refusing to guess")

    marker_offset = content.index(fallback_marker)
    line_start = content.rfind("\n", 0, marker_offset) + 1
    indentation = content[line_start:marker_offset]
    if indentation.strip():
        raise SystemExit("generic fallback indentation could not be determined")

    routes = (
        f"{indentation}redir /fleet /fleet/ 308\n\n"
        f"{indentation}@fleet_api path /fleet/api/*\n"
        f"{indentation}handle @fleet_api {{\n"
        f"{indentation}    uri strip_prefix /fleet\n"
        f"{indentation}    reverse_proxy 127.0.0.1:39461 {{\n"
        f"{indentation}        flush_interval -1\n"
        f"{indentation}    }}\n"
        f"{indentation}}}\n\n"
        f"{indentation}handle_path /fleet/* {{\n"
        f"{indentation}    reverse_proxy 127.0.0.1:39462\n"
        f"{indentation}}}\n\n"
    )
    updated = content[:line_start] + routes + content[line_start:]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = caddyfile.with_name(f"{caddyfile.name}.before-weex-fleet.{stamp}.bak")
    shutil.copy2(caddyfile, backup)

    try:
        caddyfile.write_text(updated, encoding="utf-8")
        subprocess.run(
            ["caddy", "validate", "--config", str(caddyfile)],
            check=True,
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        shutil.copy2(backup, caddyfile)
        raise

    print(f"installed WEEX Fleet routes; backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

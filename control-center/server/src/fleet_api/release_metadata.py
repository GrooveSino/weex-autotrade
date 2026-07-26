"""Read the immutable service-release identity without exposing runtime config."""

from __future__ import annotations

import json
import os
from pathlib import Path


def service_release_id(fallback_env: str) -> str:
    """Prefer the atomically switched release manifest over a stale LaunchAgent value."""
    fallback = os.environ.get(fallback_env, "").strip()
    configured_root = os.environ.get("FLEET_RELEASE_ROOT", "").strip()
    if not configured_root:
        return fallback or "dev"
    root = Path(configured_root).expanduser()
    try:
        payload = json.loads((root / "service-current" / "release.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback or "dev"
    release_id = payload.get("release_id") if isinstance(payload, dict) else None
    return release_id.strip() if isinstance(release_id, str) and release_id.strip() else fallback or "dev"

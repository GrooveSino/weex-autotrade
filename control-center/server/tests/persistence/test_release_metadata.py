from __future__ import annotations

import json

from fleet_api.release_metadata import service_release_id


def test_service_release_id_prefers_current_release_manifest(monkeypatch, tmp_path) -> None:
    manifest = tmp_path / "service-current" / "release.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"release_id": "20260726T081000Z-new"}), encoding="utf-8")
    monkeypatch.setenv("FLEET_RELEASE_ROOT", str(tmp_path))
    monkeypatch.setenv("FLEET_API_RELEASE_ID", "legacy-launch-agent-value")

    assert service_release_id("FLEET_API_RELEASE_ID") == "20260726T081000Z-new"


def test_service_release_id_falls_back_when_manifest_is_invalid(monkeypatch, tmp_path) -> None:
    manifest = tmp_path / "service-current" / "release.json"
    manifest.parent.mkdir()
    manifest.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("FLEET_RELEASE_ROOT", str(tmp_path))
    monkeypatch.setenv("FLEET_EXECUTOR_RELEASE_ID", "legacy-launch-agent-value")

    assert service_release_id("FLEET_EXECUTOR_RELEASE_ID") == "legacy-launch-agent-value"

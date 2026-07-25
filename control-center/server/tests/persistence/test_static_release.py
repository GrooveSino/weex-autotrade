from __future__ import annotations

import functools
import importlib.util
import os
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


def _static_server_module():
    script = Path(__file__).parents[3] / "scripts" / "macos" / "network" / "static_server.py"
    spec = importlib.util.spec_from_file_location("fleet_static_server", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_release(root: Path, name: str, asset_name: str) -> Path:
    release = root / name
    (release / "assets").mkdir(parents=True)
    (release / "index.html").write_text(f'<script src="/assets/{asset_name}"></script>', encoding="utf-8")
    (release / "assets" / asset_name).write_text(f"console.log({name!r})", encoding="utf-8")
    (release / "release.json").write_text(f'{{"release_id":"{name}"}}', encoding="utf-8")
    return release


def test_static_server_keeps_old_hashed_assets_available_after_atomic_switch(tmp_path: Path) -> None:
    module = _static_server_module()
    releases = tmp_path / "web-releases"
    old = _write_release(releases, "old", "index-oldhash.js")
    current = tmp_path / "web-current"
    current.symlink_to(old, target_is_directory=True)
    handler = functools.partial(module.FleetStaticHandler, root=current, releases_root=releases)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(f"{base_url}/assets/index-oldhash.js") as response:
            assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
            assert "old" in response.read().decode()

        time.sleep(0.01)
        new = _write_release(releases, "new", "index-newhash.js")
        next_link = tmp_path / "web-current.next"
        next_link.symlink_to(new, target_is_directory=True)
        os.replace(next_link, current)

        with urllib.request.urlopen(f"{base_url}/index.html") as response:
            assert response.headers["Cache-Control"] == "no-store"
            assert "index-newhash.js" in response.read().decode()
        with urllib.request.urlopen(f"{base_url}/assets/index-oldhash.js") as response:
            assert "old" in response.read().decode()
        with urllib.request.urlopen(f"{base_url}/__fleet/version.json") as response:
            assert response.headers["Cache-Control"] == "no-store"
            assert '"release_id":"new"' in response.read().decode()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

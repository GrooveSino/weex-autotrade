#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


class FleetStaticHandler(SimpleHTTPRequestHandler):
    server_version = "WEEXFleetStatic/1"

    def __init__(self, *args, root: Path, releases_root: Path | None = None, **kwargs) -> None:
        self._root = root
        self._releases_root = releases_root
        super().__init__(*args, directory=str(root), **kwargs)

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/__fleet/version.json":
            self._version()
            return
        super().do_GET()

    def end_headers(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html", "/__fleet/version.json"}:
            self.send_header("Cache-Control", "no-store")
        elif path.startswith("/assets/"):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def translate_path(self, path: str) -> str:
        current = Path(super().translate_path(path))
        if current.exists() or self._releases_root is None:
            return str(current)
        requested = unquote(urlsplit(path).path).lstrip("/")
        relative = Path(requested)
        if not requested.startswith("assets/") or relative.is_absolute() or ".." in relative.parts:
            return str(current)
        try:
            releases = sorted(
                (item for item in self._releases_root.iterdir() if item.is_dir() and not item.name.startswith(".")),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return str(current)
        for release in releases:
            candidate = release / relative
            if candidate.is_file():
                return str(candidate)
        return str(current)

    def _version(self) -> None:
        manifest_path = self._root / "release.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {
                "release_id": "development",
                "built_at": datetime.now(UTC).isoformat(),
                "api_compatibility": "v1",
                "assets": [],
                "asset_count": 0,
            }
        body = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--releases-root", type=Path)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=37642)
    args = parser.parse_args()
    root = args.root.expanduser()
    releases_root = args.releases_root.expanduser() if args.releases_root is not None else None
    if not (root / "index.html").is_file():
        raise SystemExit(f"missing static release index: {root / 'index.html'}")
    handler = lambda *handler_args, **handler_kwargs: FleetStaticHandler(  # noqa: E731
        *handler_args, root=root, releases_root=releases_root, **handler_kwargs
    )
    ThreadingHTTPServer((args.bind, args.port), handler).serve_forever()


if __name__ == "__main__":
    main()

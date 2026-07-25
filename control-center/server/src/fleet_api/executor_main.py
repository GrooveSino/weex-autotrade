from __future__ import annotations

import os
import socket as socket_module
import stat
from pathlib import Path

from fleet_api.main import create_app

app = create_app(require_command_id=True)


def bind_executor_socket(path: Path) -> socket_module.socket:
    """Bind a private, owner-only Unix socket before handing it to Uvicorn."""
    if len(os.fsencode(str(path))) >= 104:
        raise RuntimeError(f"executor socket path is too long: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if os.path.lexists(path):
        mode = path.lstat().st_mode
        if not stat.S_ISSOCK(mode):
            raise RuntimeError(f"executor socket path is not a socket: {path}")
        path.unlink()
    listener = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    try:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(128)
    except Exception:
        listener.close()
        if os.path.lexists(path):
            path.unlink()
        raise
    return listener


def run() -> None:
    import uvicorn

    raw_socket = os.environ.get("FLEET_EXECUTOR_SOCKET", "").strip()
    if not raw_socket:
        raise RuntimeError("FLEET_EXECUTOR_SOCKET is required")
    socket_path = Path(raw_socket).expanduser()
    listener = bind_executor_socket(socket_path)
    try:
        config = uvicorn.Config(app, fd=listener.fileno(), reload=False)
        uvicorn.Server(config).run()
    finally:
        listener.close()
        if os.path.lexists(socket_path):
            socket_path.unlink()


if __name__ == "__main__":
    run()

from __future__ import annotations

import argparse
import os
import secrets
import tempfile
from contextlib import suppress
from pathlib import Path

ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"


def _password() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(32))


def provision(path: Path) -> None:
    path = path.expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if path.exists():
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise RuntimeError("existing local user registry is not owner-only")
        raise RuntimeError("local user registry already exists; refusing to overwrite it")
    content = "\n".join(
        [
            "# Local-only. This file contains plaintext login passwords and must remain 0600.",
            "[users.gg]",
            f'password = "{_password()}"',
            "",
            "[users.colin]",
            f'password = "{_password()}"',
            "",
        ]
    )
    descriptor, temporary = tempfile.mkstemp(prefix=".users.", dir=path.parent, text=True)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the owner-only WEEX Fleet local user registry.")
    parser.add_argument("--path", required=True, type=Path)
    args = parser.parse_args()
    provision(args.path)


if __name__ == "__main__":
    main()

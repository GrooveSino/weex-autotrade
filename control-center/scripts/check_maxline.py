#!/usr/bin/env python3
"""Fail when Fleet source files exceed the configured line budget."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "check-maxline.json"


def load_config() -> tuple[int, set[str], set[str]]:
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    max_lines = data.get("max_lines")
    extensions = data.get("include_exts")
    excluded = data.get("exclude_dirs")
    if not isinstance(max_lines, int) or max_lines <= 0:
        raise ValueError("max_lines must be a positive integer")
    if not isinstance(extensions, list) or not all(isinstance(item, str) for item in extensions):
        raise ValueError("include_exts must be a list of strings")
    if not isinstance(excluded, list) or not all(isinstance(item, str) for item in excluded):
        raise ValueError("exclude_dirs must be a list of strings")
    return max_lines, {item.lstrip(".") for item in extensions}, set(excluded)


def count_lines(path: Path) -> int:
    with path.open("r", encoding="utf-8", errors="replace") as source:
        return sum(1 for _ in source)


def main() -> int:
    max_lines, extensions, excluded = load_config()
    failures: list[tuple[str, int]] = []
    checked = 0
    for directory, child_dirs, filenames in os.walk(ROOT, topdown=True):
        child_dirs[:] = sorted(name for name in child_dirs if name not in excluded)
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if path.suffix.lstrip(".") not in extensions:
                continue
            checked += 1
            lines = count_lines(path)
            if lines > max_lines:
                failures.append((path.relative_to(ROOT).as_posix(), lines))
    if failures:
        print(f"FAILED: {len(failures)} Fleet file(s) exceed {max_lines} lines")
        for path, lines in sorted(failures, key=lambda item: item[1], reverse=True):
            print(f"- {path}: {lines}")
        return 1
    print(f"OK: checked {checked} Fleet file(s); all are <= {max_lines} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

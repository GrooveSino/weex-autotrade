#!/usr/bin/env python3
"""Guard the ``weex_cli`` source layout during its staged decomposition.

The checked package is intentionally narrow.  Its baseline is an exact ledger
of legacy violations, so new oversize modules or directory fan-out cannot be
introduced while existing modules are being moved into focused packages.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGE_ROOT = ROOT / "src" / "weex_cli"
FLEET_ROOT = ROOT / "control-center" / "server" / "src" / "fleet_api"
CONTROL_API_ROOT = PACKAGE_ROOT / "control_api"
DEFAULT_BASELINE = ROOT / "weex_cli_structure_baseline.json"
MAX_LINES = 350
MAX_DIRECT_FILES = 8


def python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def line_count(path: Path) -> int:
    return path.read_text(encoding="utf-8", errors="replace").count("\n")


def module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join((root.name, *parts))


def module_graph(files: Iterable[Path]) -> dict[str, set[str]]:
    files = list(files)
    modules = {module_name(path, PACKAGE_ROOT): path for path in files}
    graph = {name: set() for name in modules}
    for current, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        current_parts = current.split(".")
        package_parts = current_parts if path.name == "__init__.py" else current_parts[:-1]
        for node in ast.walk(tree):
            candidates: list[str] = []
            if isinstance(node, ast.Import):
                candidates.extend(item.name for item in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = package_parts[: max(0, len(package_parts) - node.level + 1)]
                    if node.module:
                        base.extend(node.module.split("."))
                    candidates.append(".".join(base))
                elif node.module:
                    candidates.append(node.module)
            for candidate in candidates:
                if candidate in graph and candidate != current:
                    graph[current].add(candidate)
    return graph


def find_cycles(graph: dict[str, set[str]]) -> list[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    cycles: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        if node in visiting:
            start = stack.index(node)
            cycles.add(" -> ".join((*stack[start:], node)))
            return
        if node in visited:
            return
        visiting.add(node)
        stack.append(node)
        for child in graph[node]:
            visit(child, stack)
        stack.pop()
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node, [])
    return sorted(cycles)


def fleet_boundary_violations() -> list[str]:
    violations: list[str] = []
    for path in python_files(FLEET_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("weex_cli"):
                if not node.module.startswith("weex_cli.control_api"):
                    violations.append(f"private weex_cli import: {path.relative_to(ROOT)} -> {node.module}")
                for imported in node.names:
                    if imported.name.startswith("_"):
                        violations.append(
                            f"private weex_cli symbol: {path.relative_to(ROOT)} -> {node.module}.{imported.name}"
                        )
            if isinstance(node, ast.Import):
                for imported in node.names:
                    if (
                        imported.name == "weex_cli" or imported.name.startswith("weex_cli.")
                    ) and not imported.name.startswith("weex_cli.control_api"):
                        violations.append(f"private weex_cli import: {path.relative_to(ROOT)} -> {imported.name}")
    return violations


def control_api_violations() -> list[str]:
    """Keep the public Control Center boundary free of private re-exports."""
    violations: list[str] = []
    for path in python_files(CONTROL_API_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for imported in node.names:
                if imported.name.startswith("_"):
                    violations.append(
                        f"private control_api export: {path.relative_to(ROOT)} -> {node.module}.{imported.name}"
                    )
    return violations


def load_baseline(path: Path) -> tuple[dict[str, int], dict[str, int]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    files = {str(name): int(limit) for name, limit in payload.get("files", {}).items()}
    directories = {str(name): int(limit) for name, limit in payload.get("directories", {}).items()}
    return files, directories


def violations(files: list[Path], baseline: tuple[dict[str, int], dict[str, int]]) -> list[str]:
    file_baseline, directory_baseline = baseline
    failures: list[str] = []
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        limit = file_baseline.get(relative, MAX_LINES)
        actual = line_count(path)
        if actual > limit:
            failures.append(f"line limit: {relative} has {actual} lines (limit {limit})")
    for directory in sorted({path.parent for path in files}):
        relative = directory.relative_to(ROOT).as_posix()
        limit = directory_baseline.get(relative, MAX_DIRECT_FILES)
        actual = len(list(directory.glob("*.py")))
        if actual > limit:
            failures.append(f"directory limit: {relative} has {actual} Python files (limit {limit})")
    failures.extend(f"python import cycle: {cycle}" for cycle in find_cycles(module_graph(files)))
    failures.extend(fleet_boundary_violations())
    failures.extend(control_api_violations())
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--strict", action="store_true", help="Ignore the temporary legacy-debt baseline.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    baseline = ({}, {}) if args.strict else load_baseline(args.baseline)
    files = python_files(PACKAGE_ROOT)
    failures = violations(files, baseline)
    if failures:
        print(f"FAILED: {len(failures)} weex_cli structure violation(s)")
        print("\n".join(f"- {failure}" for failure in failures))
        return 1
    status = "strict" if args.strict else "baseline-locked"
    print(f"OK ({status}): {len(files)} files; line <= {MAX_LINES}; direct files <= {MAX_DIRECT_FILES}; no cycles")
    return 0


if __name__ == "__main__":
    sys.exit(main())

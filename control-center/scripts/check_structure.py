#!/usr/bin/env python3
"""Validate Fleet source layout, line budgets, and local import cycles."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_EXTENSIONS = {".py", ".ts", ".tsx", ".css", ".zsh", ".mjs"}
EXCLUDED_PARTS = {".git", ".venv", "__pycache__", "dist", "node_modules", ".pytest_cache", ".ruff_cache"}
MAX_LINES = 350
MAX_DIRECT_FILES = 8


def source_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in SOURCE_EXTENSIONS
        and not EXCLUDED_PARTS.intersection(path.parts)
    )


def resolve_python_module(module: str, modules: set[str]) -> str | None:
    if module in modules:
        return module
    package = f"{module}.__init__"
    return module if package in modules else None


def python_graph(files: list[Path]) -> dict[str, set[str]]:
    source_root = ROOT / "server" / "src"
    module_by_file: dict[Path, str] = {}
    for path in files:
        if source_root not in path.parents or path.suffix != ".py":
            continue
        relative = path.relative_to(source_root).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        module_by_file[path] = ".".join(parts)
    modules = set(module_by_file.values())
    graph = {module: set() for module in modules}
    for path, current in module_by_file.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        package = current.rsplit(".", 1)[0] if "." in current else current
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                candidates = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base_parts = package.split(".")
                    if node.level > len(base_parts):
                        continue
                    base = ".".join(base_parts[: len(base_parts) - node.level + 1])
                    candidates = [".".join(part for part in (base, node.module or "") if part)]
                else:
                    candidates = [node.module or ""]
            else:
                continue
            for candidate in candidates:
                if candidate == "fleet_api":
                    continue
                resolved = resolve_python_module(candidate, modules)
                if resolved and resolved != current:
                    graph[current].add(resolved)
    return graph


IMPORT_RE = re.compile(r"(?:from|import)\s+['\"](\.[^'\"]+)['\"]")


def resolve_typescript_import(source: Path, specifier: str, modules: set[Path]) -> Path | None:
    candidate = (source.parent / specifier).resolve()
    options = [candidate, *[candidate.with_suffix(ext) for ext in (".ts", ".tsx")]]
    options.extend(candidate / f"index{ext}" for ext in (".ts", ".tsx"))
    return next((item for item in options if item in modules), None)


def typescript_graph(files: list[Path]) -> dict[Path, set[Path]]:
    source_root = ROOT / "src"
    modules = {path.resolve() for path in files if source_root in path.parents and path.suffix in {".ts", ".tsx"}}
    graph = {path: set() for path in modules}
    for path in modules:
        for specifier in IMPORT_RE.findall(path.read_text(encoding="utf-8")):
            target = resolve_typescript_import(path, specifier, modules)
            if target and target != path:
                graph[path].add(target)
    return graph


def cycle_nodes(graph: dict[object, set[object]]) -> list[str]:
    visiting: set[object] = set()
    visited: set[object] = set()
    cycles: set[str] = set()

    def visit(node: object) -> None:
        if node in visiting:
            cycles.add(str(node))
            return
        if node in visited:
            return
        visiting.add(node)
        for child in graph.get(node, set()):
            visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)
    return sorted(cycles)


def main() -> int:
    files = source_files(ROOT)
    failures: list[str] = []
    for path in files:
        lines = path.read_text(encoding="utf-8", errors="replace").count("\n")
        if lines > MAX_LINES:
            failures.append(f"line limit: {path.relative_to(ROOT)} has {lines} lines")
    directories = {path.parent for path in files}
    for directory in sorted(directories):
        direct = [path for path in files if path.parent == directory]
        if len(direct) > MAX_DIRECT_FILES:
            failures.append(f"directory limit: {directory.relative_to(ROOT)} has {len(direct)} source files")
    python_cycles = cycle_nodes(python_graph(files))
    typescript_cycles = cycle_nodes(typescript_graph(files))
    failures.extend(f"python import cycle: {item}" for item in python_cycles)
    failures.extend(f"typescript import cycle: {item}" for item in typescript_cycles)
    if failures:
        print(f"FAILED: {len(failures)} structure violation(s)")
        print("\n".join(f"- {item}" for item in failures))
        return 1
    print(f"OK: {len(files)} source files; line <= {MAX_LINES}; direct files <= {MAX_DIRECT_FILES}; no local cycles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

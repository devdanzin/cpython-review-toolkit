#!/usr/bin/env python3
"""Shared utilities for cpython-review-toolkit analysis scripts.

Provides common infrastructure used by the tree-sitter-based scanners:
CPython root detection, C file discovery, CLI argument parsing, comment-based
suppression, and finding deduplication.

The generic helpers here are kept structurally in sync with the sibling
toolkits' ``scan_common.py`` (cext / ft) so shared fixes propagate; the
CPython-specific piece is :func:`find_cpython_root`, which keys off the
CPython source-tree markers rather than a ``.git`` / ``pyproject.toml`` root.
"""

import json
import re
import sys
from collections.abc import Generator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tree_sitter_utils import (
    C_EXTENSIONS,
    get_node_text,
)

EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".tox",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        ".eggs",
        "egg-info",
    }
)


# ---------------------------------------------------------------------------
# CPython root detection + file discovery
# ---------------------------------------------------------------------------


def find_cpython_root(start: Path) -> Path | None:
    """Walk up from ``start`` to the CPython source-tree root.

    A CPython checkout is identified by the co-presence of ``Include/Python.h``
    and ``Objects/object.c``. Returns None if no such root is found within 20
    parent levels (callers fall back to the scan directory).
    """
    current = start if start.is_dir() else start.parent
    for _ in range(20):
        if (current / "Include" / "Python.h").is_file() and (
            current / "Objects" / "object.c"
        ).is_file():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def resolve_roots(target: str) -> tuple[Path, Path]:
    """Return ``(project_root, scan_root)`` for a target path.

    ``project_root`` is the CPython root if one is found (so finding paths are
    reported relative to the checkout root, e.g. ``Objects/tupleobject.c``);
    otherwise it falls back to the scan directory. ``scan_root`` is the
    directory actually walked for ``.c`` / ``.h`` files.
    """
    target_path = Path(target).resolve()
    scan_root = target_path if target_path.is_dir() else target_path.parent
    project_root = find_cpython_root(target_path) or scan_root
    return project_root, scan_root


def relpath(filepath: Path, project_root: Path) -> str:
    """Best-effort path of ``filepath`` relative to ``project_root``."""
    try:
        return str(filepath.relative_to(project_root))
    except ValueError:
        return str(filepath)


def discover_c_files(
    root: Path,
    *,
    max_files: int = 0,
) -> Generator[Path, None, None]:
    """Yield C source/header files under ``root``, skipping build dirs."""
    count = 0
    if root.is_file():
        if root.suffix in C_EXTENSIONS:
            yield root
        return
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix not in C_EXTENSIONS:
            continue
        try:
            parts = set(p.relative_to(root).parts)
        except ValueError:
            continue
        if parts & EXCLUDE_DIRS:
            continue
        yield p
        count += 1
        if max_files and count >= max_files:
            return


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


def parse_common_args(argv: list[str]) -> tuple[str, int]:
    """Parse common CLI arguments (positional path and ``--max-files N``).

    Returns ``(target_path, max_files)``. ``max_files == 0`` means unlimited.
    """
    max_files = 0
    positional: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--max-files" and i + 1 < len(argv):
            try:
                max_files = int(argv[i + 1])
            except ValueError:
                print(
                    json.dumps(
                        {
                            "error": (
                                f"--max-files requires an integer, got '{argv[i + 1]}'"
                            )
                        }
                    )
                )
                sys.exit(2)
            i += 2
        elif argv[i].startswith("--"):
            i += 1
        else:
            positional.append(argv[i])
            i += 1
    target = positional[0] if positional else "."
    return target, max_files


# ---------------------------------------------------------------------------
# Report envelope
# ---------------------------------------------------------------------------


def build_report(
    *,
    project_root: Path,
    scan_root: Path,
    files_analyzed: int,
    functions_analyzed: int,
    findings: list[dict],
    summary: dict,
    **extra: object,
) -> dict:
    """Assemble the standard JSON report envelope shared across scanners."""
    report: dict = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_analyzed": files_analyzed,
        "functions_analyzed": functions_analyzed,
        "findings": findings,
        "summary": summary,
    }
    report.update(extra)
    return report


# ---------------------------------------------------------------------------
# Comment-based suppression (parity with cext/ft scan_common)
# ---------------------------------------------------------------------------

_SAFETY_KEYWORDS = frozenset(
    {
        "safety:",
        "safe because",
        "intentional",
        "by design",
        "cpython-safe:",
        "nolint",
        "checked:",
        "correct because",
        "this is safe",
        "not a bug",
        "deliberately",
        "already checked",
        "gil held",
        "gil-held",
        "cannot fail",
        "can't fail",
    }
)


def extract_nearby_comments(
    source_bytes: bytes, tree, line: int, radius: int = 5
) -> list[str]:
    """Return comment texts within ±``radius`` lines of a 1-indexed ``line``."""
    comments: list[str] = []
    min_line = max(0, line - radius - 1)  # 0-indexed
    max_line = line + radius - 1

    def _walk(node) -> None:
        if node.type == "comment":
            node_line = node.start_point[0]
            if min_line <= node_line <= max_line:
                comments.append(get_node_text(node, source_bytes))
        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return comments


def has_safety_annotation(comments: list[str]) -> bool:
    """True if any comment carries a human safety/suppression annotation."""
    for comment in comments:
        lower = comment.lower()
        if any(kw in lower for kw in _SAFETY_KEYWORDS):
            return True
    return False


def is_suppressed_by_comment(
    source_bytes: bytes, tree, line: int, radius: int = 3
) -> bool:
    """True if a finding at ``line`` is annotated as intentional nearby."""
    return has_safety_annotation(
        extract_nearby_comments(source_bytes, tree, line, radius)
    )


# ---------------------------------------------------------------------------
# Finding deduplication (parity with cext scan_common)
# ---------------------------------------------------------------------------


def deduplicate_findings(findings: list[dict]) -> list[dict]:
    """Collapse near-identical findings by (type, file, normalized detail).

    Keeps the first occurrence as canonical and records the rest under
    ``duplicate_count`` / ``duplicate_locations`` — useful when a systemic
    pattern (e.g. a copy-pasted slot) repeats across many sites.
    """

    def _normalize_detail(detail: str) -> str:
        text = re.sub(r"line \d+", "line N", detail)
        text = re.sub(r"'[^']+?'", "'VAR'", text)
        return text

    groups: dict[tuple[str, str, str], list[dict]] = {}
    for f in findings:
        key = (
            f.get("type", ""),
            f.get("file", ""),
            _normalize_detail(f.get("detail", "")),
        )
        groups.setdefault(key, []).append(f)

    result: list[dict] = []
    for group in groups.values():
        canonical = group[0]
        if len(group) > 1:
            canonical["duplicate_count"] = len(group) - 1
            canonical["duplicate_locations"] = [
                {"file": d.get("file", ""), "line": d.get("line", 0)} for d in group[1:]
            ]
        result.append(canonical)
    return result

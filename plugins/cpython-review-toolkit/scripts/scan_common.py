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
    otherwise it falls back to the target's directory. ``scan_root`` is what is
    actually walked for ``.c`` / ``.h`` files.

    ``scan_root`` is the target **itself**, file or directory. It used to be
    ``target_path.parent`` for a file target, which silently widened the scan:
    ``scan_pyerr_clear.py Modules/_interpretersmodule.c`` scanned all 400+ files
    of ``Modules/`` and reported findings the caller never asked about.
    :func:`discover_c_files` already handles a file root by yielding just that
    file, so passing the target through is both correct and simpler.
    """
    target_path = Path(target).resolve()
    scan_root = target_path
    # project_root must still be a *directory* when there is no CPython root.
    fallback_root = target_path if target_path.is_dir() else target_path.parent
    project_root = find_cpython_root(target_path) or fallback_root
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


def parse_common_args(
    argv: list[str], *, known_flags: frozenset[str] | None = None
) -> tuple[str, int]:
    """Parse common CLI arguments (positional path and ``--max-files N``).

    Returns ``(target_path, max_files)``. ``max_files == 0`` means unlimited.

    Unrecognised ``--flags`` are **warned about on stderr** rather than silently
    dropped. Silently ignoring a flag is how ``analyze_history.py --months 420``
    came to run with the default 90-day window and report a confident, wrong
    analysis; a scan that ignores ``--max-files-typo`` has the same failure mode.
    Callers that legitimately accept extra flags pass them in ``known_flags`` so
    they are not reported.

    Warnings go to stderr, never stdout, because stdout carries the JSON report.
    """
    known = known_flags or frozenset()
    max_files = 0
    positional: list[str] = []
    unknown: list[str] = []
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
            flag = argv[i].split("=", 1)[0]
            if flag not in known:
                unknown.append(argv[i])
            i += 1
        else:
            positional.append(argv[i])
            i += 1
    if unknown:
        print(
            f"warning: ignoring unrecognised option(s): {' '.join(unknown)}",
            file=sys.stderr,
        )
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
    """Assemble the standard JSON report envelope shared across scanners.

    When a scanner passes ``vocabulary_counts`` and every count is zero, the
    envelope gains ``rule_not_applicable: true``. That distinction is the one
    this toolkit keeps getting wrong: an empty ``findings`` list means either
    "the constructs are present and clean" or "the scanner recognised nothing
    here", and those are opposite conclusions. Four v0.8 rules produced
    structural zeros that were recorded as clean bills, and the obj-typeobject
    review found scan_gil_usage resolving 0 constructs in a file with 11
    stop-the-world regions and 3 critical-section families. A reader should not
    have to sum a dict to notice.
    """
    report: dict = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_analyzed": files_analyzed,
        "functions_analyzed": functions_analyzed,
        "findings": findings,
        "summary": summary,
    }
    report.update(extra)

    counts = report.get("vocabulary_counts")
    if isinstance(counts, dict) and counts:
        resolved = sum(v for v in counts.values() if isinstance(v, (int, float)))
        report["rule_not_applicable"] = resolved == 0
        if resolved == 0:
            report["rule_not_applicable_note"] = (
                "This scanner recognised NONE of its vocabulary in this scope, so "
                "the empty findings list is silence, not safety. Either the "
                "constructs genuinely are absent, or they are spelled in a way "
                "the vocabulary does not cover (a file-local macro wrapper is the "
                "usual cause). Establish which before reporting a clean result."
            )
    report["denominators"] = collect_denominators(report)
    return report


# Envelope / summary keys that answer "how much did this rule even see". Named
# by suffix so a scanner gains a denominator by naming its counter well, rather
# than by editing a list here.
_DENOMINATOR_SUFFIXES = (
    "_sites",
    "_functions",
    "_resolved",
    "_analyzed",
    "_census",
    "_candidates",
    "_wrappers",
    "_helpers",
    "_fields",
)


def collect_denominators(report: dict) -> dict:
    """Gather every "how much did the rule see" counter into one block.

    ``rule_not_applicable`` answers the question only for scanners that ship a
    vocabulary. The rest express their reach as bespoke envelope keys
    (``total_nullable_fields``, ``allocation_sites``,
    ``critical_section_functions``, …), which an agent has to know about in
    advance to look for. Collecting them under one name means the standing rule
    -- *report the denominator before calling a zero clean* -- can be followed
    against any scanner's output without knowing which scanner it is.

    Reported alongside ``findings``, so the ratio is readable at a glance.
    """
    out: dict = {
        "files_analyzed": report.get("files_analyzed", 0),
        "functions_analyzed": report.get("functions_analyzed", 0),
        "findings": len(report.get("findings") or ()),
    }
    counts = report.get("vocabulary_counts")
    if isinstance(counts, dict):
        out["vocabulary_resolved"] = sum(
            v for v in counts.values() if isinstance(v, (int, float))
        )
        out["vocabulary_tokens_seen"] = len(counts)

    for source in (report, report.get("summary") or {}):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if key in out or not key.endswith(_DENOMINATOR_SUFFIXES):
                continue
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                out[key] = value
            elif isinstance(value, dict) and value and all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                for v in value.values()
            ):
                # A census dict holds the counts themselves; reporting its
                # *length* would say "3" for a three-key census whose numbers
                # are 161/127/27, which is worse than saying nothing.
                for sub, subvalue in value.items():
                    out.setdefault(f"{key}.{sub}", subvalue)
            elif isinstance(value, (list, dict)):
                out[key] = len(value)

    measured = [
        v for k, v in out.items() if k != "findings" and isinstance(v, (int, float))
    ]
    if measured and not any(measured):
        out["note"] = (
            "Every denominator this scanner reports is zero: the empty findings "
            "list is silence, not safety. Establish that the constructs are "
            "genuinely absent before reporting a clean result."
        )
    return out


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
    """Collapse only **exact** duplicates, keyed on ``(type, file, line)``.

    This used to group on a *normalized* detail string that rewrote ``line \\d+``
    to ``line N`` and every ``'quoted'`` token to ``'VAR'`` — i.e. it erased the
    variable and function names that distinguish two findings. Two genuinely
    distinct bugs of the same shape in one file therefore collapsed into one,
    with the second demoted to ``duplicate_locations`` and effectively hidden.

    That was measured, not theoretical: ``xibufferview_dealloc``
    (``Modules/_interpretersmodule.c``) has unguarded ``PyErr_Clear()`` calls at
    both :175 and :183, and the second vanished; the same happened to
    ``bytearrayobject.c:1171`` / ``:1177``. Silently dropping a real finding is
    far worse than emitting two similar ones, so exactness wins.

    A systemic copy-paste pattern is still visible — it is simply reported as N
    findings at N distinct lines, which is what a reviewer needs to fix them all.
    """
    groups: dict[tuple[str, str, int], list[dict]] = {}
    for f in findings:
        key = (f.get("type", ""), f.get("file", ""), int(f.get("line", 0) or 0))
        groups.setdefault(key, []).append(f)

    result: list[dict] = []
    for group in groups.values():
        canonical = group[0]
        if len(group) > 1:
            # Same type at the same file:line — a genuine re-emission.
            canonical["duplicate_count"] = len(group) - 1
        result.append(canonical)
    return result

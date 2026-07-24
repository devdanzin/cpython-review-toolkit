#!/usr/bin/env python3
"""Scan CPython C source for exception-clobbering in the destructor family.

The dangerous pattern (OOM class O3): a ``PyErr_Clear()`` inside a
``tp_dealloc`` / ``tp_clear`` / ``tp_finalize`` / ``tp_traverse`` function with
no surrounding save/restore of the exception state. Destructors and finalizers
run while an exception may already be in flight (an object's last reference is
commonly dropped mid-exception-handling), so an unguarded ``PyErr_Clear()``
there silently swallows the caller's live ``MemoryError`` / ``KeyboardInterrupt``
/ ``SystemExit``.

The correct idiom brackets the risky work with a save/restore pair
(``PyErr_GetRaisedException`` / ``PyErr_SetRaisedException``, the older
``PyErr_Fetch`` / ``PyErr_Restore``, or reports via ``PyErr_WriteUnraisable``);
functions that do so are treated as guarded and suppressed.

Confirmed exemplars this targets: ``context_tp_dealloc`` (Modules/_contextvars,
gh-152083), ``subtype_dealloc`` (Objects/typeobject.c), ``deque_clear``
(Modules/_collectionsmodule.c).

Usage:
    python scan_pyerr_clear.py [path] [--max-files N]
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_common import (
    build_report,
    deduplicate_findings,
    discover_c_files,
    is_suppressed_by_comment,
    parse_common_args,
    relpath,
    resolve_roots,
)
from tree_sitter_utils import (
    extract_functions,
    get_node_text,
    parse_bytes,
    walk_descendants,
)

# Function-name suffixes that identify the destructor / GC family. The slot
# kind carried alongside is used to weight severity (a swallow in dealloc/
# finalize/clear is worse than in traverse, which should be side-effect-free
# anyway — a PyErr_Clear there is unusual but worth surfacing).
_DESTRUCTOR_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_dealloc", "tp_dealloc"),
    ("_finalize", "tp_finalize"),
    ("_clear", "tp_clear"),
    ("_traverse", "tp_traverse"),
)

# Exact names that don't follow the suffix convention.
_DESTRUCTOR_EXACT = {
    "subtype_dealloc": "tp_dealloc",
    "dealloc": "tp_dealloc",
}

# Slot assignments in a static PyTypeObject / PyType_Slot table map a function
# to a destructor slot even when its name doesn't advertise the role.
_SLOT_DESIGNATED_RE = re.compile(
    r"\.tp_(dealloc|finalize|clear|traverse)\s*=\s*(?:\(\s*\w[\w\s\*]*\)\s*)?"
    r"(?:&\s*)?(\w+)"
)
_SLOT_SPEC_RE = re.compile(
    r"\{\s*Py_tp_(dealloc|finalize|clear|traverse)\s*,\s*(?:&\s*)?(\w+)\s*\}"
)

# The presence of any of these in the function body means the author is
# managing the exception state deliberately — suppress.
_SAVE_RESTORE_APIS = frozenset(
    {
        "PyErr_Fetch",
        "PyErr_Restore",
        "PyErr_GetRaisedException",
        "PyErr_SetRaisedException",
        "PyErr_GetHandledException",
        "PyErr_SetHandledException",
        "PyErr_WriteUnraisable",
        "_PyErr_WriteUnraisableMsg",
        "_PyErr_ChainExceptions",
        "_PyErr_ChainExceptions1",
        "PyErr_FormatUnraisable",
    }
)


def _collect_destructor_slot_map(source: str) -> dict[str, str]:
    """Map function name -> destructor slot kind from static type tables."""
    slot_map: dict[str, str] = {}
    for slot, name in _SLOT_DESIGNATED_RE.findall(source):
        slot_map[name] = f"tp_{slot}"
    for slot, name in _SLOT_SPEC_RE.findall(source):
        slot_map[name] = f"tp_{slot}"
    return slot_map


def _destructor_kind(func_name: str, slot_map: dict[str, str]) -> str | None:
    """Return the destructor slot kind for a function, or None."""
    if func_name in slot_map:
        return slot_map[func_name]
    if func_name in _DESTRUCTOR_EXACT:
        return _DESTRUCTOR_EXACT[func_name]
    for suffix, kind in _DESTRUCTOR_SUFFIXES:
        if func_name.endswith(suffix):
            return kind
    return None


def _find_pyerr_clear_calls(func: dict, source_bytes: bytes) -> list[dict]:
    """Find every ``PyErr_Clear()`` call in a function body."""
    calls: list[dict] = []
    for node in walk_descendants(func["body_node"], type_filter="call_expression"):
        fn_node = node.child_by_field_name("function")
        if fn_node and get_node_text(fn_node, source_bytes) == "PyErr_Clear":
            calls.append({"node": node, "line": node.start_point[0] + 1})
    return calls


def _has_save_restore_guard(func: dict) -> bool:
    """True if the function manages exception state via a save/restore API."""
    body = func["body"]
    return any(api in body for api in _SAVE_RESTORE_APIS)


def _check_function(func: dict, kind: str, source_bytes: bytes, tree) -> list[dict]:
    """Flag unguarded ``PyErr_Clear()`` in a destructor-family function."""
    clears = _find_pyerr_clear_calls(func, source_bytes)
    if not clears:
        return []
    if _has_save_restore_guard(func):
        return []

    findings: list[dict] = []
    # dealloc/finalize/clear run during teardown that may be mid-exception;
    # traverse is meant to be side-effect-free, so a clear there is odd but
    # lower-severity.
    confidence = "medium" if kind == "tp_traverse" else "high"
    for call in clears:
        if is_suppressed_by_comment(source_bytes, tree, call["line"]):
            continue
        findings.append(
            {
                "type": "pyerr_clear_in_dealloc",
                "function": func["name"],
                "slot": kind,
                "line": call["line"],
                "confidence": confidence,
                "detail": (
                    f"PyErr_Clear() in {kind} function '{func['name']}' with no "
                    "surrounding save/restore of the exception state — silently "
                    "swallows an in-flight MemoryError / KeyboardInterrupt / "
                    "SystemExit. Bracket the risky work with "
                    "PyErr_GetRaisedException()/PyErr_SetRaisedException() (or "
                    "report via PyErr_WriteUnraisable)."
                ),
            }
        )
    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan for exception-clobbering PyErr_Clear() in the destructor family."""
    project_root, scan_root = resolve_roots(target)

    findings: list[dict] = []
    total_functions = 0
    total_clears = 0
    destructor_functions = 0
    files_analyzed = 0
    skipped: list[dict] = []

    for filepath in discover_c_files(scan_root, max_files=max_files):
        try:
            source_bytes = filepath.read_bytes()
        except OSError as e:
            skipped.append({"file": str(filepath), "reason": str(e)})
            continue

        try:
            tree = parse_bytes(source_bytes)
        except Exception as e:  # pragma: no cover - defensive
            skipped.append({"file": str(filepath), "reason": f"parse: {e}"})
            continue

        functions = extract_functions(tree, source_bytes)
        if not functions:
            continue

        files_analyzed += 1
        rel = relpath(filepath, project_root)
        source = source_bytes.decode("utf-8", errors="replace")
        slot_map = _collect_destructor_slot_map(source)

        for func in functions:
            total_functions += 1
            kind = _destructor_kind(func["name"], slot_map)
            if kind is None:
                continue
            destructor_functions += 1
            clears = _find_pyerr_clear_calls(func, source_bytes)
            total_clears += len(clears)
            for f in _check_function(func, kind, source_bytes, tree):
                f["file"] = rel
                findings.append(f)

    findings = deduplicate_findings(findings)

    by_confidence: dict[str, int] = defaultdict(int)
    by_slot: dict[str, int] = defaultdict(int)
    for f in findings:
        by_confidence[f["confidence"]] += 1
        by_slot[f["slot"]] += 1

    return build_report(
        project_root=project_root,
        scan_root=scan_root,
        files_analyzed=files_analyzed,
        functions_analyzed=total_functions,
        findings=findings,
        summary={
            "total_findings": len(findings),
            "by_confidence": dict(by_confidence),
            "by_slot": dict(by_slot),
        },
        destructor_functions=destructor_functions,
        total_pyerr_clear_calls_in_destructors=total_clears,
        skipped_files=skipped,
    )


def main() -> None:
    try:
        target, max_files = parse_common_args(sys.argv[1:])
        result = analyze(target, max_files=max_files)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:  # pragma: no cover - defensive
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()

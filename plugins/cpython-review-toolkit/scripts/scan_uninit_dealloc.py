#!/usr/bin/env python3
"""Scan CPython C source for dealloc of a half-constructed object on error.

The dangerous pattern (bug class B / O5): a constructor allocates an object
with a **non-zeroing** allocator (``PyObject_New`` / ``PyObject_GC_New`` /
``PyObject_NewVar`` …), then — before initializing the object's pointer members
to NULL — hits a fallible step and frees the object on the error path
(``Py_DECREF`` / ``Py_XDECREF`` / ``Py_CLEAR``). The ``tp_dealloc`` / ``tp_clear``
then reads those still-uninitialized members (garbage pointers) and crashes.
Under out-of-memory this is the dominant reachable-from-Python crash surface.

The correct idiom either zeroes the object right after allocation (``memset(op,
0, ...)`` or a run of ``op->member = NULL;`` before the first fallible call) or
uses a zeroing allocator (``tp_alloc`` / ``PyType_GenericAlloc`` /
``*_GC_Calloc``). Constructors that do so are treated as safe.

Confirmed exemplars: ``template_iter`` dealloc of an uninitialized member
(Objects/templateobject.c, gh-151815), blake2 ``.copy()`` freeing an object
with an uninitialized ``impl`` enum (Modules/blake2, gh-152851).

Usage:
    python scan_uninit_dealloc.py [path] [--max-files N]
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
    find_assignments_in_scope,
    parse_bytes,
)

# Allocators that do NOT zero the type-specific fields of the new object.
_NON_ZEROING_ALLOCATORS = (
    "PyObject_New",
    "PyObject_NewVar",
    "PyObject_GC_New",
    "PyObject_GC_NewVar",
    "_PyObject_New",
    "_PyObject_NewVar",
    "_PyObject_GC_New",
    "_PyObject_GC_NewVar",
    "PyObject_GC_NewWithExtra",
)

_NON_ZEROING_RE = re.compile(
    r"^\s*(?:\(\s*[\w\s\*]+\)\s*)?(" + "|".join(_NON_ZEROING_ALLOCATORS) + r")\s*\("
)


def _member_null_init_re(var: str) -> re.Pattern:
    return re.compile(rf"\b{re.escape(var)}\s*->\s*\w+\s*=\s*NULL")


def _member_write_re(var: str) -> re.Pattern:
    return re.compile(rf"\b{re.escape(var)}\s*->\s*\w+\s*=")


def _memset_zero_re(var: str) -> re.Pattern:
    return re.compile(
        rf"memset\s*\(\s*(?:\(\s*void\s*\*\s*\)\s*)?{re.escape(var)}\b[^;]*,\s*0\s*,"
    )


def _early_free_re(var: str) -> re.Pattern:
    return re.compile(rf"Py_(?:X?DECREF|CLEAR)\s*\(\s*{re.escape(var)}\b")


def _check_function(func: dict, source_bytes: bytes, tree) -> list[dict]:
    """Flag non-zeroing constructors that free the object before init."""
    findings: list[dict] = []
    body = func["body"]
    body_start = func["body_node"].start_byte + 1  # +1: leading '{' stripped

    for assign in find_assignments_in_scope(func["body_node"], source_bytes):
        var = assign["variable"]
        if not var.isidentifier():
            continue
        m = _NON_ZEROING_RE.match(assign["value_text"])
        if not m:
            continue
        api = m.group(1)

        offset = assign["value_node"].start_byte - body_start
        if offset < 0 or offset > len(body):
            continue
        tail = body[offset:]

        # Object explicitly zeroed after allocation -> safe.
        if _memset_zero_re(var).search(tail):
            continue
        # Locate the first early free of the object on an error path. Without
        # one, nothing is flagged (the only decref is the caller's, on the
        # successfully returned object).
        free_m = _early_free_re(var).search(tail)
        if free_m is None:
            continue
        # The object must carry pointer members a dealloc would touch.
        if not _member_write_re(var).search(tail):
            continue
        # Correct pattern: members NULL-initialized (or the object memset)
        # before it is freed, so tp_dealloc sees NULL (Py_XDECREF(NULL) is
        # safe) rather than garbage. The allocation's own `if (var == NULL)`
        # guard doesn't free var, so this boundary is the real error path.
        pre_free = tail[: free_m.start()]
        if _member_null_init_re(var).search(pre_free):
            continue

        line = assign["start_line"]
        if is_suppressed_by_comment(source_bytes, tree, line):
            continue

        findings.append(
            {
                "type": "dealloc_of_uninitialized_object",
                "function": func["name"],
                "variable": var,
                "allocator": api,
                "line": line,
                "confidence": "medium",
                "detail": (
                    f"'{var}' is allocated via the non-zeroing {api}() and freed "
                    f"on an error path (Py_DECREF/Py_XDECREF/Py_CLEAR) before its "
                    f"members are NULL-initialized — tp_dealloc/tp_clear then "
                    f"reads uninitialized member pointers (crash, esp. under "
                    f"OOM). Zero the object right after allocation (memset or "
                    f"`{var}->member = NULL;` before the first fallible call)."
                ),
            }
        )

    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan for constructors that free half-initialized objects on error."""
    project_root, scan_root = resolve_roots(target)

    findings: list[dict] = []
    total_functions = 0
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

        for func in functions:
            total_functions += 1
            for f in _check_function(func, source_bytes, tree):
                f["file"] = rel
                findings.append(f)

    findings = deduplicate_findings(findings)

    by_allocator: dict[str, int] = defaultdict(int)
    for f in findings:
        by_allocator[f["allocator"]] += 1

    return build_report(
        project_root=project_root,
        scan_root=scan_root,
        files_analyzed=files_analyzed,
        functions_analyzed=total_functions,
        findings=findings,
        summary={
            "total_findings": len(findings),
            "by_allocator": dict(by_allocator),
        },
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

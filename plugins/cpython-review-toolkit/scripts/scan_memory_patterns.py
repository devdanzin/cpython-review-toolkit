#!/usr/bin/env python3
"""Scan CPython C source for memory-management bugs beyond reference counting.

Three syntactic checks, each with a distinct finding ``type``. All default to
silence — a candidate surfaces only when a specific, high-signal shape matches.

1. ``alloc_size_overflow`` (bug class R5; cf. gh-3493, gh-1779)
   A ``PyMem_Malloc(n * size)`` / ``malloc(count * sizeof(T))`` /
   ``PyMem_Realloc(p, n * k)`` where a multiply operand derives from a
   **Python-controlled** value (a ``PyLong_As*`` / ``PyObject_Length`` /
   ``Py_SIZE`` result, or a ``PyArg_Parse*`` output) and there is **no** prior
   bounds guard (a ``PY_SSIZE_T_MAX / size`` division check, a ``< 0`` sign
   check, ``__builtin_mul_overflow`` …). ``PyMem_New`` / ``PyMem_Resize`` /
   ``*_Calloc`` do the overflow check internally and are treated as SAFE — they
   never reach this check because their size arguments are separate operands,
   not a bare ``a * b``.

2. ``gc_untrack_without_track`` (bug class O6; cf. gh-152107 OOM-0006/0017)
   A constructor allocates with ``PyObject_GC_New*`` and, on an early error
   path, frees the object (``Py_DECREF`` / ``Py_XDECREF`` / ``Py_CLEAR``)
   *before* any ``PyObject_GC_Track`` on it. If ``tp_dealloc`` then runs the
   untrack macro (``_PyObject_GC_UNTRACK``) on the never-tracked object the GC
   invariant is violated.

3. ``mismatched_alloc_free`` (allocator-family mismatch)
   The SAME variable is allocated by one allocator family and freed by another
   in the same function (``PyMem_Malloc`` freed with ``free`` / ``PyObject_Free``,
   ``PyObject_Malloc`` freed with ``PyMem_Free``, ``malloc`` freed with
   ``PyMem_Free`` …). The three families (raw ``malloc``, ``PyMem_*``,
   ``PyObject_*``) draw from different heaps and must not be crossed.

Usage:
    python scan_memory_patterns.py [path] [--max-files N]
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
    find_calls_in_scope,
    get_node_text,
    parse_bytes,
    walk_descendants,
)

# ---------------------------------------------------------------------------
# Check 1: integer overflow in an allocation size
# ---------------------------------------------------------------------------

# Allocators whose size argument is a single expression we can inspect for a
# Python-derived multiply. calloc-family (PyMem_Calloc / PyObject_Calloc /
# calloc) and PyMem_New / PyMem_Resize are omitted deliberately: they take the
# count and element size as *separate* arguments and overflow-check internally.
# {allocator: index of the size argument}
_SIZE_ARG_INDEX = {
    "malloc": 0,
    "PyMem_Malloc": 0,
    "PyMem_RawMalloc": 0,
    "PyObject_Malloc": 0,
    "realloc": 1,
    "PyMem_Realloc": 1,
    "PyMem_RawRealloc": 1,
    "PyObject_Realloc": 1,
}

# Calls whose result is a Python-controlled integer. Used both as
# return-assignment taint sources and as inline operands inside the multiply
# (the call's name appears as an identifier in the sub-tree).
_TAINT_CALL_NAMES = frozenset(
    {
        "PyLong_AsSsize_t",
        "PyLong_AsLong",
        "PyLong_AsLongLong",
        "PyLong_AsSize_t",
        "PyLong_AsUnsignedLong",
        "PyLong_AsUnsignedLongLong",
        "PyLong_AsUnsignedLongMask",
        "PyNumber_AsSsize_t",
        "PyObject_Length",
        "PyObject_Size",
        "PyObject_LengthHint",
        "PySequence_Length",
        "PySequence_Size",
        "PyMapping_Length",
        "PyMapping_Size",
        "PyTuple_Size",
        "PyTuple_GET_SIZE",
        "PyList_Size",
        "PyList_GET_SIZE",
        "PyBytes_Size",
        "PyBytes_GET_SIZE",
        "PyByteArray_Size",
        "PyByteArray_GET_SIZE",
        "PyUnicode_GetLength",
        "PyUnicode_GET_LENGTH",
        "Py_SIZE",
    }
)

_PYARG_PARSERS = frozenset(
    {
        "PyArg_ParseTuple",
        "PyArg_ParseTupleAndKeywords",
        "PyArg_Parse",
        "PyArg_ParseStack",
        "PyArg_VaParse",
        "_PyArg_ParseTupleAndKeywords",
        "_PyArg_ParseStack",
        "_PyArg_ParseStackAndKeywords",
    }
)

# Textual signals that an overflow guard precedes the allocation. Any of these
# in the pre-allocation body text suppresses the finding (deliberately
# over-broad — silence beats noise on a tree this large).
_MAX_CONSTS = (
    "PY_SSIZE_T_MAX",
    "PY_SIZE_MAX",
    "SIZE_MAX",
    "SSIZE_MAX",
    "INT_MAX",
    "UINT_MAX",
    "LONG_MAX",
    "ULONG_MAX",
    "SIZE_T_MAX",
)
_OVERFLOW_HELPERS = (
    "__builtin_mul_overflow",
    "__builtin_add_overflow",
    "_Py_size_",
    "_Py_memory_repeat",
    "size_overflow",
    "overflow",
)

_LEADING_CAST_RE = re.compile(r"^\s*(?:\(\s*[\w\s\*]+\)\s*)+")
_HEAD_CALL_RE = re.compile(r"([A-Za-z_]\w*)\s*\(")
_BARE_IDENT_RE = re.compile(r"^\s*(?:\(\s*[\w\s\*]+\)\s*)*([A-Za-z_]\w*)\s*$")
_ADDR_IDENT_RE = re.compile(r"&\s*([A-Za-z_]\w*)")


def _head_call(value_text: str) -> str | None:
    """Return the leading call identifier of an expression, past one cast run."""
    s = _LEADING_CAST_RE.sub("", value_text.strip())
    m = _HEAD_CALL_RE.match(s)
    return m.group(1) if m else None


def _bare_ident(text: str) -> str | None:
    """Return the identifier if ``text`` is a (possibly cast) single ident."""
    m = _BARE_IDENT_RE.match(text)
    return m.group(1) if m else None


def _collect_taint(func: dict, source_bytes: bytes) -> tuple[set[str], set[str]]:
    """Return (strong_tainted_vars, weak_tainted_vars) for a function body.

    Strong: assigned from a PyLong_As* / length / Py_SIZE call.
    Weak: written through ``&var`` by a PyArg_Parse* call (broad; lower signal).
    """
    strong: set[str] = set()
    weak: set[str] = set()

    for assign in find_assignments_in_scope(func["body_node"], source_bytes):
        var = assign["variable"]
        if not var.isidentifier():
            continue
        head = _head_call(assign["value_text"])
        if head in _TAINT_CALL_NAMES:
            strong.add(var)

    for call in find_calls_in_scope(func["body_node"], source_bytes, _PYARG_PARSERS):
        for m in _ADDR_IDENT_RE.finditer(call["arguments_text"]):
            weak.add(m.group(1))

    return strong, weak


def _multiplies(node, source_bytes: bytes):
    """Yield binary_expression nodes whose operator is ``*`` within ``node``."""
    for bx in walk_descendants(node, "binary_expression"):
        op = bx.child_by_field_name("operator")
        if op is not None and get_node_text(op, source_bytes) == "*":
            yield bx


def _has_overflow_guard(pre_text: str, variables: set[str]) -> bool:
    """True if the pre-allocation text carries any overflow / bounds guard."""
    if any(c in pre_text for c in _MAX_CONSTS):
        return True
    if any(h in pre_text for h in _OVERFLOW_HELPERS):
        return True
    for v in variables:
        vre = re.escape(v)
        if re.search(rf"\b{vre}\b\s*(?:<|<=|>|>=|==|!=)\s*0\b", pre_text):
            return True
        if re.search(rf"\b0\b\s*(?:<|<=|>|>=|==|!=)\s*\b{vre}\b", pre_text):
            return True
    return False


def _check_alloc_size_overflow(func: dict, source_bytes: bytes, tree) -> list[dict]:
    strong, weak = _collect_taint(func, source_bytes)
    if not strong and not weak:
        return []

    body = func["body"]
    body_start = func["body_node"].start_byte + 1

    findings: list[dict] = []
    for call in find_calls_in_scope(func["body_node"], source_bytes):
        idx = _SIZE_ARG_INDEX.get(call["function_name"])
        if idx is None:
            continue
        args_node = call["node"].child_by_field_name("arguments")
        if args_node is None:
            continue
        arg_nodes = [c for c in args_node.named_children if c.type != "comment"]
        if idx >= len(arg_nodes):
            continue
        size_arg = arg_nodes[idx]

        for bx in _multiplies(size_arg, source_bytes):
            idents = {
                get_node_text(n, source_bytes)
                for n in walk_descendants(bx, "identifier")
            }
            strong_hit = idents & (strong | _TAINT_CALL_NAMES)
            weak_hit = idents & weak
            if not strong_hit and not weak_hit:
                continue

            guard_vars = (strong_hit - _TAINT_CALL_NAMES) | weak_hit
            offset = call["start_byte"] - body_start
            pre_text = body[:offset] if 0 <= offset <= len(body) else body
            if _has_overflow_guard(pre_text, guard_vars):
                continue

            line = call["start_line"]
            if is_suppressed_by_comment(source_bytes, tree, line):
                continue

            confidence = "medium" if strong_hit else "low"
            operand = ", ".join(sorted(idents & (strong | weak | _TAINT_CALL_NAMES)))
            findings.append(
                {
                    "type": "alloc_size_overflow",
                    "function": func["name"],
                    "line": line,
                    "confidence": confidence,
                    "detail": (
                        f"{call['function_name']}() size argument multiplies a "
                        f"Python-controlled operand ({operand}) with no visible "
                        f"overflow guard (no PY_SSIZE_T_MAX/size division check, "
                        f"< 0 sign check, or __builtin_mul_overflow before the "
                        f"call). The product can wrap, under-allocating the "
                        f"buffer (bug class R5; cf. gh-3493, gh-1779). Use "
                        f"PyMem_New/PyMem_Calloc (they overflow-check) or add an "
                        f"explicit `n > PY_SSIZE_T_MAX / size` guard."
                    ),
                }
            )
    return findings


# ---------------------------------------------------------------------------
# Check 2: GC-track invariant (free before GC_Track)
# ---------------------------------------------------------------------------

_GC_ALLOCATORS = frozenset(
    {
        "PyObject_GC_New",
        "PyObject_GC_NewVar",
        "_PyObject_GC_New",
        "_PyObject_GC_NewVar",
        "PyObject_GC_NewWithExtra",
    }
)
_GC_TRACK_NAMES = frozenset(
    {"PyObject_GC_Track", "_PyObject_GC_TRACK", "PyObject_GC_TRACK"}
)
_EARLY_FREE_NAMES = frozenset({"Py_DECREF", "Py_XDECREF", "Py_CLEAR"})

# The O6 bug is only reachable when tp_dealloc runs the untrack *macro*
# (_PyObject_GC_UNTRACK), which unconditionally unlinks an object it assumes is
# tracked. The public *function* PyObject_GC_UnTrack guards with
# _PyObject_GC_IS_TRACKED and is safe on a never-tracked object, so the
# ubiquitous "Py_DECREF(op) on error before GC_Track" idiom is correct wherever
# the untrack macro is absent. Gating on macro presence (below) suppresses the
# entire safe-idiom population — nearly all of Modules/ uses only the function.
_GC_UNTRACK_MACRO = b"_PyObject_GC_UNTRACK"


def _check_gc_untrack_without_track(
    func: dict, source_bytes: bytes, tree
) -> list[dict]:
    # File-level gate: no untrack macro anywhere -> no tp_dealloc in this file
    # can hit the O6 shape, so the free-before-track idiom here is safe.
    if _GC_UNTRACK_MACRO not in source_bytes:
        return []

    body_node = func["body_node"]

    # Objects allocated with a GC allocator (var -> allocator, first assign).
    gc_objs: dict[str, str] = {}
    for assign in find_assignments_in_scope(body_node, source_bytes):
        var = assign["variable"]
        if not var.isidentifier() or var in gc_objs:
            continue
        head = _head_call(assign["value_text"])
        if head in _GC_ALLOCATORS:
            gc_objs[var] = head
    if not gc_objs:
        return []

    # Earliest GC_Track byte per object.
    track_off: dict[str, int] = {}
    for call in find_calls_in_scope(body_node, source_bytes, _GC_TRACK_NAMES):
        ident = _bare_ident(call["arguments_text"])
        if ident in gc_objs and ident not in track_off:
            track_off[ident] = call["start_byte"]

    # Earliest early-free byte per object.
    free_info: dict[str, dict] = {}
    for call in find_calls_in_scope(body_node, source_bytes, _EARLY_FREE_NAMES):
        ident = _bare_ident(call["arguments_text"])
        if ident in gc_objs and ident not in free_info:
            free_info[ident] = {
                "byte": call["start_byte"],
                "line": call["start_line"],
                "func": call["function_name"],
            }

    findings: list[dict] = []
    for var, allocator in gc_objs.items():
        free = free_info.get(var)
        if free is None:
            continue
        track = track_off.get(var)
        # Flag only when the free precedes every track of this object (or no
        # track exists at all): the object can reach tp_dealloc's untrack macro
        # having never been placed in the GC list.
        if track is not None and track <= free["byte"]:
            continue
        line = free["line"]
        if is_suppressed_by_comment(source_bytes, tree, line):
            continue
        findings.append(
            {
                "type": "gc_untrack_without_track",
                "function": func["name"],
                "line": line,
                "confidence": "low",
                "detail": (
                    f"'{var}' is allocated via {allocator}() and freed with "
                    f"{free['func']}() on an error path before any "
                    f"PyObject_GC_Track({var}) — if tp_dealloc runs the untrack "
                    f"macro (_PyObject_GC_UNTRACK) on the never-tracked object "
                    f"the GC invariant is violated (bug class O6; cf. gh-152107 "
                    f"OOM-0006/0017). Ensure the error path does not reach an "
                    f"unconditional untrack, or track before the fallible step."
                ),
            }
        )
    return findings


# ---------------------------------------------------------------------------
# Check 3: mismatched alloc/free family
# ---------------------------------------------------------------------------

_ALLOC_FAMILY = {
    "malloc": "raw",
    "calloc": "raw",
    "realloc": "raw",
    "reallocarray": "raw",
    "strdup": "raw",
    "PyMem_Malloc": "pymem",
    "PyMem_Calloc": "pymem",
    "PyMem_Realloc": "pymem",
    "PyMem_New": "pymem",
    "PyMem_RawMalloc": "pymem_raw",
    "PyMem_RawCalloc": "pymem_raw",
    "PyMem_RawRealloc": "pymem_raw",
    "PyObject_Malloc": "pyobject",
    "PyObject_Calloc": "pyobject",
    "PyObject_Realloc": "pyobject",
}
_FREE_FAMILY = {
    "free": "raw",
    "PyMem_Free": "pymem",
    "PyMem_Del": "pymem",
    "PyMem_DEL": "pymem",
    "PyMem_RawFree": "pymem_raw",
    "PyObject_Free": "pyobject",
    "PyObject_Del": "pyobject",
    "PyObject_DEL": "pyobject",
    "PyObject_GC_Del": "pyobject",
}
_FAMILY_LABEL = {
    "raw": "malloc/free",
    "pymem": "PyMem_*",
    "pymem_raw": "PyMem_Raw*",
    "pyobject": "PyObject_*",
}


def _check_mismatched_alloc_free(func: dict, source_bytes: bytes, tree) -> list[dict]:
    body_node = func["body_node"]

    alloc_family: dict[str, set[str]] = defaultdict(set)
    alloc_line: dict[str, int] = {}
    for assign in find_assignments_in_scope(body_node, source_bytes):
        var = assign["variable"]
        if not var.isidentifier():
            continue
        head = _head_call(assign["value_text"])
        fam = _ALLOC_FAMILY.get(head)
        if fam is not None:
            alloc_family[var].add(fam)
            alloc_line.setdefault(var, assign["start_line"])

    if not alloc_family:
        return []

    findings: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for call in find_calls_in_scope(body_node, source_bytes, frozenset(_FREE_FAMILY)):
        free_fam = _FREE_FAMILY[call["function_name"]]
        ident = _bare_ident(call["arguments_text"])
        if ident is None:
            continue
        families = alloc_family.get(ident)
        # Require an unambiguous single allocation family for the variable.
        if not families or len(families) != 1:
            continue
        alloc_fam = next(iter(families))
        if alloc_fam == free_fam:
            continue
        key = (ident, alloc_fam, free_fam)
        if key in seen:
            continue
        seen.add(key)

        line = call["start_line"]
        if is_suppressed_by_comment(source_bytes, tree, line):
            continue
        findings.append(
            {
                "type": "mismatched_alloc_free",
                "function": func["name"],
                "line": line,
                "confidence": "high",
                "detail": (
                    f"'{ident}' is allocated with the "
                    f"{_FAMILY_LABEL[alloc_fam]} family but freed with "
                    f"{call['function_name']}() ({_FAMILY_LABEL[free_fam]}). "
                    f"CPython's three allocator families draw from different "
                    f"heaps and must not be crossed — undefined behavior. Free "
                    f"with the matching family."
                ),
            }
        )
    return findings


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _check_function(func: dict, source_bytes: bytes, tree) -> list[dict]:
    findings: list[dict] = []
    findings.extend(_check_alloc_size_overflow(func, source_bytes, tree))
    findings.extend(_check_gc_untrack_without_track(func, source_bytes, tree))
    findings.extend(_check_mismatched_alloc_free(func, source_bytes, tree))
    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan for allocation-size overflow, GC-track, and allocator-mismatch bugs."""
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

    by_type: dict[str, int] = defaultdict(int)
    by_confidence: dict[str, int] = defaultdict(int)
    for f in findings:
        by_type[f["type"]] += 1
        by_confidence[f["confidence"]] += 1

    return build_report(
        project_root=project_root,
        scan_root=scan_root,
        files_analyzed=files_analyzed,
        functions_analyzed=total_functions,
        findings=findings,
        summary={
            "total_findings": len(findings),
            "by_type": dict(by_type),
            "by_confidence": dict(by_confidence),
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

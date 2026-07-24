#!/usr/bin/env python3
"""Scan CPython C source for free-threading data races in its own code.

Targets the three most mechanically-detectable race classes from the fusil
`cpython-tsan-findings` catalog. CPython's free-threaded build (``Py_GIL_DISABLED``)
makes these real, reachable-from-Python crashes, not hypotheticals.

* **T3 — iterator-exhaustion double-DECREF** (the fixed-template, ~zero-FP one):
  a ``*_iternext`` slot drops an owning reference to a shared self-member
  (``Py_CLEAR(it->it_seq)`` / ``it->it_seq = NULL; Py_DECREF(seq);``) with no
  critical section. Two ``next()`` threads read the same borrowed sequence and
  both DECREF it → double-free. Confirmed: dict iter (gh-154130), set iter
  (gh-144357), StringIO iter (gh-153296).

* **T2 — lazy-init cache without a critical section**: ``if (!self->f) self->f =
  compute();`` with no critical section in the function — two threads both see
  NULL, both compute and store (leak + torn/lost write). Confirmed:
  ``descr_get_qualname`` (Objects/descrobject.c, TSAN-0043).

* **T1 — atomic/plain access asymmetry**: a struct field accessed via an atomic
  macro (``_Py_atomic_*`` / ``FT_ATOMIC_*``) at one site and as a plain
  ``x->field`` load/store at another in the same file. The asymmetry means one
  access is unsynchronized. Confirmed: itertools ``count_repr`` (TSAN-0006).

Usage:
    python scan_ft_races.py [path] [--max-files N]
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
    parse_bytes,
)

# A function has "some locking" if any of these appear — covers
# Py_BEGIN_CRITICAL_SECTION / _SECTION2 / _SECTION_MUTEX (all share the prefix).
_LOCK_TOKENS = ("Py_BEGIN_CRITICAL_SECTION", "PyMutex_Lock", "_PyCriticalSection")

# Atomic access macros (CPython's own FT wrappers + the low-level primitives).
_ATOMIC_ACCESS_RE = re.compile(
    r"(?:_Py_atomic_\w+|FT_ATOMIC_\w+)\s*\(\s*&?\s*\w+\s*->\s*(\w+)"
)
_PLAIN_MEMBER_RE = re.compile(r"\b(\w+)\s*->\s*(\w+)\b")

# iternext slot wiring (designated init + PyType_Slot spec form).
_ITERNEXT_DESIGNATED_RE = re.compile(
    r"\.tp_iternext\s*=\s*(?:\(\s*\w[\w\s\*]*\)\s*)?(?:&\s*)?(\w+)"
)
_ITERNEXT_SPEC_RE = re.compile(r"\{\s*Py_tp_iternext\s*,\s*(?:&\s*)?(\w+)\s*\}")

# T3: dropping an owning ref to a self-member.
_MEMBER_CLEAR_RE = re.compile(r"Py_CLEAR\s*\(\s*\w+\s*->\s*\w+\s*\)")
_MEMBER_SET_NULL_RE = re.compile(r"\b\w+\s*->\s*\w+\s*=\s*NULL\b")
_DECREF_RE = re.compile(r"\bPy_X?DECREF\s*\(")

# T2: lazy-init of a self-member guarded only by a NULL check.
_LAZY_INIT_RE = re.compile(
    r"if\s*\(\s*(?:!\s*)?(\w+)\s*->\s*(\w+)\s*(?:==\s*NULL\s*)?\)\s*"
    r"(?:\{)?[^;{}]*?\1\s*->\s*\2\s*=\s*(?!=)",
    re.DOTALL,
)


def _has_lock(body: str) -> bool:
    return any(tok in body for tok in _LOCK_TOKENS)


def _caller_holds_lock(func_name: str) -> bool:
    """CPython FT convention: a ``*_lock_held`` function runs with the critical
    section already held by its caller, so the lock is not missing here."""
    return func_name.endswith(("_lock_held", "_locked"))


def _collect_iternext_names(source: str) -> set[str]:
    names = set(_ITERNEXT_DESIGNATED_RE.findall(source))
    names |= set(_ITERNEXT_SPEC_RE.findall(source))
    return names


def _is_iternext(func_name: str, slot_names: set[str]) -> bool:
    return func_name in slot_names or "iternext" in func_name


def _line_of(body: str, match_start: int, func_start_line: int) -> int:
    return func_start_line + body[:match_start].count("\n")


def _check_t3(func: dict, is_iter: bool, tree, source_bytes: bytes) -> dict | None:
    """T3: iternext that drops an owning self-member ref without a lock."""
    if not is_iter or _caller_holds_lock(func["name"]):
        return None
    body = func["body"]
    if _has_lock(body):
        return None

    clear_m = _MEMBER_CLEAR_RE.search(body)
    drop_start: int | None = None
    if clear_m:
        drop_start = clear_m.start()
    else:
        setnull_m = _MEMBER_SET_NULL_RE.search(body)
        if setnull_m is not None and _DECREF_RE.search(body):
            drop_start = setnull_m.start()
    if drop_start is None:
        return None

    line = _line_of(body, drop_start, func["start_line"])
    if is_suppressed_by_comment(source_bytes, tree, line):
        return None
    return {
        "type": "iternext_double_decref",
        "ft_class": "T3",
        "function": func["name"],
        "line": line,
        "confidence": "high",
        "detail": (
            f"'{func['name']}' is a tp_iternext that drops an owning reference "
            "to a shared self-member (Py_CLEAR / member=NULL + Py_DECREF) with "
            "no Py_BEGIN_CRITICAL_SECTION. Under the free-threaded build two "
            "concurrent next() calls read the same borrowed sequence and both "
            "DECREF it -> double-free (cf. gh-154130 dict, gh-144357 set, "
            "gh-153296 StringIO). Wrap the iternext body in "
            "Py_BEGIN_CRITICAL_SECTION(self)."
        ),
    }


def _check_t2(func: dict, tree, source_bytes: bytes) -> list[dict]:
    """T2: lazy-init of a self-member guarded only by a NULL check."""
    if _caller_holds_lock(func["name"]):
        return []
    body = func["body"]
    if _has_lock(body):
        return []
    findings: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for m in _LAZY_INIT_RE.finditer(body):
        obj, field = m.group(1), m.group(2)
        if (obj, field) in seen:
            continue
        seen.add((obj, field))
        line = _line_of(body, m.start(), func["start_line"])
        if is_suppressed_by_comment(source_bytes, tree, line):
            continue
        findings.append(
            {
                "type": "lazy_init_no_critical_section",
                "ft_class": "T2",
                "function": func["name"],
                "member": f"{obj}->{field}",
                "line": line,
                "confidence": "medium",
                "detail": (
                    f"Lazy init of '{obj}->{field}' guarded only by a NULL "
                    "check, with no critical section. Under free-threading two "
                    "threads both observe NULL and both compute/store -> leak + "
                    "torn/lost write (cf. gh descr_get_qualname / TSAN-0043). "
                    "Guard with Py_BEGIN_CRITICAL_SECTION or a compare-exchange."
                ),
            }
        )
    return findings


def _check_t1(source: str, rel: str) -> list[dict]:
    """T1: a field accessed atomically at one site and plainly at another."""
    atomic_fields = set(_ATOMIC_ACCESS_RE.findall(source))
    if not atomic_fields:
        return []

    # Byte ranges covered by atomic-macro calls, so plain accesses inside an
    # atomic call are not counted as "plain".
    atomic_spans = [(m.start(), m.end()) for m in _ATOMIC_ACCESS_RE.finditer(source)]

    def _in_atomic(pos: int) -> bool:
        return any(s <= pos < e for s, e in atomic_spans)

    findings: list[dict] = []
    reported: set[str] = set()
    for m in _PLAIN_MEMBER_RE.finditer(source):
        field = m.group(2)
        if field not in atomic_fields or field in reported:
            continue
        if _in_atomic(m.start()):
            continue
        reported.add(field)
        line = source[: m.start()].count("\n") + 1
        findings.append(
            {
                "type": "atomic_plain_asymmetry",
                "ft_class": "T1",
                "file": rel,
                "member": field,
                "line": line,
                "confidence": "low",
                "detail": (
                    f"Field '{field}' is accessed via an atomic macro "
                    "(_Py_atomic_* / FT_ATOMIC_*) elsewhere in this file but is "
                    f"read/written plainly here — one access is unsynchronized "
                    "(cf. itertools count_repr / TSAN-0006). Make every access "
                    "to a concurrently-shared field atomic, or hold a lock."
                ),
            }
        )
    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan for free-threading data races (T1/T2/T3) in CPython's own code."""
    project_root, scan_root = resolve_roots(target)

    findings: list[dict] = []
    total_functions = 0
    iternext_functions = 0
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
        source = source_bytes.decode("utf-8", errors="replace")
        # T1 is file-scoped (needs cross-function field access map).
        for f in _check_t1(source, relpath(filepath, project_root)):
            findings.append(f)
        if not functions:
            continue

        files_analyzed += 1
        rel = relpath(filepath, project_root)
        slot_names = _collect_iternext_names(source)

        for func in functions:
            total_functions += 1
            is_iter = _is_iternext(func["name"], slot_names)
            if is_iter:
                iternext_functions += 1
            t3 = _check_t3(func, is_iter, tree, source_bytes)
            if t3 is not None:
                t3["file"] = rel
                findings.append(t3)
            for f in _check_t2(func, tree, source_bytes):
                f["file"] = rel
                findings.append(f)

    findings = deduplicate_findings(findings)

    by_class: dict[str, int] = defaultdict(int)
    by_confidence: dict[str, int] = defaultdict(int)
    for f in findings:
        by_class[f["ft_class"]] += 1
        by_confidence[f["confidence"]] += 1

    return build_report(
        project_root=project_root,
        scan_root=scan_root,
        files_analyzed=files_analyzed,
        functions_analyzed=total_functions,
        findings=findings,
        summary={
            "total_findings": len(findings),
            "by_class": dict(by_class),
            "by_confidence": dict(by_confidence),
        },
        iternext_functions=iternext_functions,
        skipped_files=skipped,
    )


def main() -> None:
    try:
        target, max_files = parse_common_args(sys.argv[1:])
        result = analyze(target, max_files=max_files)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:  # noqa: BLE001
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Scan CPython C source for critical-section / lock-discipline issues.

CPython's own ``Objects/``, ``Modules/`` and ``Python/`` use per-object
critical sections pervasively for free-threading (PEP 703): dozens of
``Py_BEGIN_CRITICAL_SECTION(op)`` / ``Py_END_CRITICAL_SECTION()`` pairs live in
``Objects/dictobject.c`` alone. The macros are *scoped*: ``BEGIN`` opens a
brace and declares a stack-local ``PyCriticalSection`` that ``END`` pops. Leave
the section on any path without the matching ``END`` and the per-object lock is
never released — a deadlock the moment two threads contend the object.

This scanner focuses on the two highest-signal shapes on CPython's own code:

* ``critical_section_missing_end`` / ``critical_section_end_on_error`` (FIX) —
  a ``Py_BEGIN_CRITICAL_SECTION`` (or the ``2`` / ``_MUTEX`` spelling) with no
  matching ``Py_END`` on some path, typically an early ``return`` or an
  out-of-section ``goto`` sitting between the begin and its end.
* ``nested_critical_sections`` (CONSIDER) — two *different* objects locked at
  once via two single-object begins instead of the deadlock-safe
  ``Py_BEGIN_CRITICAL_SECTION2(a, b)``.

The common, correct idiom (begin ... work ... end on every path) is silent.
Analysis is intra-function only: a section opened in one function and closed in
another is out of scope and honestly not modelled.

Usage:
    python scan_lock_discipline.py [path] [--max-files N]
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
    find_calls_in_scope,
    find_return_statements,
    get_node_text,
    parse_bytes,
    walk_descendants,
)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# The single-object begin/end spellings. The ``_2`` variant locks two objects
# together (canonical order, deadlock-safe); the ``_MUTEX`` variant is
# CPython's mutex-backed begin, ``Py_BEGIN_CRITICAL_SECTION_MUTEX(&m)``, paired
# with the ordinary ``Py_END_CRITICAL_SECTION()``. ``_MUTEX`` is absent from the
# verbatim sibling ``lock_macros.json`` (that file stays byte-for-byte in sync
# with ft-review-toolkit), so it is spliced in here at the code level.
_CS_BEGIN_1 = "Py_BEGIN_CRITICAL_SECTION"
_CS_BEGIN_2 = "Py_BEGIN_CRITICAL_SECTION2"
_CS_MUTEX_BEGIN = "Py_BEGIN_CRITICAL_SECTION_MUTEX"
_CS_END_1 = "Py_END_CRITICAL_SECTION"
_CS_END_2 = "Py_END_CRITICAL_SECTION2"

_lock_data: dict | None = None


def _load_lock_macros() -> dict:
    """Load the (verbatim-vendored) lock-macro vocabulary from data/."""
    global _lock_data
    if _lock_data is not None:
        return _lock_data
    try:
        with open(_DATA_DIR / "lock_macros.json", encoding="utf-8") as f:
            _lock_data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:  # pragma: no cover - defensive
        print(f"Warning: failed to load lock_macros.json: {e}", file=sys.stderr)
        _lock_data = {}
    return _lock_data


def _get_critical_section_pair() -> tuple[set[str], set[str]]:
    """Return ``(begin_macros, end_macros)`` for the critical-section family.

    Sourced from the ``type == "critical_section"`` entry of the verbatim
    ``lock_macros.json`` plus the CPython-only ``_MUTEX`` begin spelling.
    """
    begins: set[str] = set()
    ends: set[str] = set()
    for pair in _load_lock_macros().get("lock_pairs", []):
        if pair.get("type") == "critical_section":
            begins.update(pair.get("acquire", []))
            ends.update(pair.get("release", []))
    # Tolerate the mutex-backed begin CPython also uses.
    begins.add(_CS_MUTEX_BEGIN)
    return begins, ends


def _matching_end(begin_name: str) -> str:
    """Name of the ``END`` macro that closes a given ``BEGIN`` spelling."""
    if begin_name == _CS_BEGIN_2:
        return _CS_END_2
    return _CS_END_1


def _norm_args(args: str) -> str:
    """Whitespace-insensitive normalization of a macro's argument text."""
    return re.sub(r"\s+", "", args)


# ---------------------------------------------------------------------------
# Per-function analysis
# ---------------------------------------------------------------------------


def _collect_labels(func_node, source_bytes: bytes) -> dict[str, int]:
    """Map ``label name -> start byte`` for every label in the function."""
    labels: dict[str, int] = {}
    for node in walk_descendants(func_node, "labeled_statement"):
        label_node = node.child_by_field_name("label")
        if label_node is not None:
            labels[get_node_text(label_node, source_bytes)] = node.start_byte
    return labels


def _collect_exits(func_node, source_bytes: bytes) -> list[dict]:
    """Collect every ``return`` and ``goto`` in the function (byte-ordered)."""
    exits: list[dict] = []
    for ret in find_return_statements(func_node, source_bytes):
        exits.append(
            {
                "kind": "return",
                "byte": ret["node"].start_byte,
                "line": ret["start_line"],
                "label": None,
            }
        )
    for node in walk_descendants(func_node, "goto_statement"):
        label_node = node.child_by_field_name("label")
        exits.append(
            {
                "kind": "goto",
                "byte": node.start_byte,
                "line": node.start_point[0] + 1,
                "label": get_node_text(label_node, source_bytes)
                if label_node is not None
                else None,
            }
        )
    return exits


def _analyze_critical_sections(func: dict, source_bytes: bytes) -> list[dict]:
    """Flag missing / early-exited / nested critical sections in one function."""
    begins_set, ends_set = _get_critical_section_pair()
    calls = find_calls_in_scope(func["body_node"], source_bytes, begins_set | ends_set)
    if not calls:
        return []

    events = sorted(
        (
            {
                "name": c["function_name"],
                "line": c["start_line"],
                "byte": c["start_byte"],
                "args": c["arguments_text"],
                "is_begin": c["function_name"] in begins_set,
                "end_byte": None,
            }
            for c in calls
        ),
        key=lambda e: e["byte"],
    )

    findings: list[dict] = []

    # LIFO-pair begins with ends. While pairing, flag two different objects
    # held at once (nested single-object begins) as a deadlock risk.
    stack: list[dict] = []
    for ev in events:
        if ev["is_begin"]:
            if stack:
                outer = stack[-1]
                if (
                    ev["name"] != _CS_BEGIN_2
                    and outer["name"] != _CS_BEGIN_2
                    and _norm_args(ev["args"]) != _norm_args(outer["args"])
                    and outer["args"]
                    and ev["args"]
                ):
                    findings.append(_nested_finding(func, outer, ev))
            stack.append(ev)
        elif stack:
            stack[-1]["end_byte"] = ev["byte"]
            stack.pop()

    # A begin left on the stack has no matching end on any path.
    ended: list[dict] = []
    for ev in events:
        if not ev["is_begin"]:
            continue
        if ev["end_byte"] is None:
            findings.append(_missing_end_finding(func, ev))
        else:
            ended.append(ev)

    # A return/goto strictly between a begin and its matching end leaves the
    # section open on that path. A goto whose target label is itself inside the
    # section (e.g. a `retry:` loop) is an internal jump, not an exit.
    labels = _collect_labels(func["body_node"], source_bytes)
    exits = _collect_exits(func["body_node"], source_bytes)
    for begin in ended:
        e_byte = begin["end_byte"]
        for exit_ in exits:
            if not begin["byte"] < exit_["byte"] < e_byte:
                continue
            if exit_["kind"] == "goto":
                target = labels.get(exit_["label"] or "")
                if target is not None and begin["byte"] < target < e_byte:
                    continue
            findings.append(_end_on_error_finding(func, begin, exit_))

    return findings


# ---------------------------------------------------------------------------
# Finding builders
# ---------------------------------------------------------------------------


def _missing_end_finding(func: dict, begin: dict) -> dict:
    end = _matching_end(begin["name"])
    return {
        "type": "critical_section_missing_end",
        "function": func["name"],
        "line": begin["line"],
        "classification": "FIX",
        "confidence": "high",
        "detail": (
            f"{begin['name']}({begin['args']}) at line {begin['line']} in "
            f"'{func['name']}' has no matching {end}() on any path — the per-object "
            f"lock is never released, so the object stays locked (deadlock under "
            f"free-threading). Add {end}() before the function returns."
        ),
    }


def _end_on_error_finding(func: dict, begin: dict, exit_: dict) -> dict:
    end = _matching_end(begin["name"])
    where = (
        f"return at line {exit_['line']}"
        if exit_["kind"] == "return"
        else f"goto {exit_['label']} at line {exit_['line']}"
    )
    return {
        "type": "critical_section_end_on_error",
        "function": func["name"],
        "line": exit_["line"],
        "classification": "FIX",
        "confidence": "high",
        "detail": (
            f"{where} in '{func['name']}' leaves the critical section opened by "
            f"{begin['name']}({begin['args']}) at line {begin['line']} without "
            f"calling {end}() first — the per-object lock leaks on this path. Call "
            f"{end}() before the exit (or restructure so the exit is inside the "
            f"section's END)."
        ),
    }


def _nested_finding(func: dict, outer: dict, inner: dict) -> dict:
    return {
        "type": "nested_critical_sections",
        "function": func["name"],
        "line": inner["line"],
        "classification": "CONSIDER",
        "confidence": "medium",
        "detail": (
            f"'{func['name']}' opens {inner['name']}({inner['args']}) at line "
            f"{inner['line']} while the section from {outer['name']}({outer['args']}) "
            f"at line {outer['line']} is still held — two different objects locked at "
            f"once. If another thread locks them in the opposite order this "
            f"deadlocks; use Py_BEGIN_CRITICAL_SECTION2({outer['args']}, "
            f"{inner['args']}) to acquire both in a canonical order."
        ),
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _has_critical_section(func: dict) -> bool:
    """Cheap check: does the function mention any critical-section begin?"""
    # ``Py_BEGIN_CRITICAL_SECTION`` is a prefix of the ``2`` and ``_MUTEX``
    # spellings, so one substring test covers all three.
    return _CS_BEGIN_1 in func["body"]


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan for critical-section / lock-discipline issues in CPython C source."""
    project_root, scan_root = resolve_roots(target)

    findings: list[dict] = []
    files_analyzed = 0
    total_functions = 0
    cs_functions = 0
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
            if _has_critical_section(func):
                cs_functions += 1
            for f in _analyze_critical_sections(func, source_bytes):
                if is_suppressed_by_comment(source_bytes, tree, f["line"]):
                    continue
                f["file"] = rel
                findings.append(f)

    findings = deduplicate_findings(findings)

    by_type: dict[str, int] = defaultdict(int)
    by_classification: dict[str, int] = defaultdict(int)
    for f in findings:
        by_type[f["type"]] += 1
        by_classification[f["classification"]] += 1

    return build_report(
        project_root=project_root,
        scan_root=scan_root,
        files_analyzed=files_analyzed,
        functions_analyzed=total_functions,
        findings=findings,
        summary={
            "total_findings": len(findings),
            "by_type": dict(by_type),
            "by_classification": dict(by_classification),
        },
        critical_section_functions=cs_functions,
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

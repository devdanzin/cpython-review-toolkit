#!/usr/bin/env python3
"""Scan CPython C source for recursion-prone slots that lack a recursion guard.

The dangerous pattern (bug class A / R1): a function wired to a recursion-prone
type slot — ``tp_hash`` / ``tp_richcompare`` / ``tp_repr`` / ``tp_str`` — or a
container parameter walk (``*make_parameters*`` / ``*subs_parameters*``) that
descends into a Python object graph *without* ``Py_EnterRecursiveCall`` /
``Py_ReprEnter``. A deeply-nested or cyclic object then overflows the **C
stack** (SIGSEGV) instead of raising ``RecursionError``.

Two descent shapes are detected:

* **self-recursion** — the function calls itself with no guard (the
  ``_Py_make_parameters`` class, gh-154275).
* **container element-descent** — the function loops over a container and calls
  a recursion-prone element op (``PyObject_Hash`` / ``PyObject_Repr`` /
  ``PyObject_RichCompare`` …) with no guard (the ``tuple_hash`` /
  ``frozendict_hash`` class, gh-154318 — noted upstream as a copy-pasted
  guardless algorithm).

The presence of any recursion-guard macro in the body suppresses the finding.

Usage:
    python scan_recursion_guards.py [path] [--max-files N]
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
    parse_bytes,
    walk_descendants,
)

# Map a function name -> slot from static type tables (designated + spec form).
# Recursion-prone slots (tp_hash / tp_richcompare / tp_repr / tp_str) descend
# the object graph on user-controlled input.
_SLOT_DESIGNATED_RE = re.compile(
    r"\.tp_(hash|richcompare|repr|str)\s*=\s*(?:\(\s*\w[\w\s\*]*\)\s*)?"
    r"(?:&\s*)?(\w+)"
)
_SLOT_SPEC_RE = re.compile(
    r"\{\s*Py_tp_(hash|richcompare|repr|str)\s*,\s*(?:&\s*)?(\w+)\s*\}"
)

# Name-suffix heuristics for the same slots (many CPython slots are named
# <type>_hash / <type>_repr / ... and never appear in a designated init).
_SLOT_NAME_SUFFIX: tuple[tuple[str, str], ...] = (
    ("_richcompare", "tp_richcompare"),
    ("_hash", "tp_hash"),
    ("_repr", "tp_repr"),
    ("_str", "tp_str"),
)

# The parameter-walk class (PEP 585 generic alias): recurses over nested
# type arguments; not a slot, but the same native-stack-overflow shape.
_PARAM_WALK_RE = re.compile(r"(?:make|subs)_parameters", re.IGNORECASE)

# Element operations that recurse back into the object graph. A container slot
# that calls one of these on its items is descending element-wise.
_ELEMENT_RECURSION_APIS = frozenset(
    {
        "PyObject_Hash",
        "PyObject_Repr",
        "PyObject_Str",
        "PyObject_ASCII",
        "PyObject_RichCompare",
        "PyObject_RichCompareBool",
    }
)

# Container-iteration signals (used with element ops to distinguish a
# per-element descent from a single bounded attribute access).
_CONTAINER_ACCESS_RE = re.compile(
    r"\b\w*_GET_ITEM\b|\b\w*_GET_SIZE\b|\bPySequence_Fast_GET_ITEM\b"
)

# Recursion-guard macros. Any of these in the body discharges the obligation.
_GUARD_TOKENS = frozenset(
    {
        "Py_EnterRecursiveCall",
        "_Py_EnterRecursiveCall",
        "Py_EnterRecursiveCallTstate",
        "_Py_EnterRecursiveCallTstate",
        "Py_ReprEnter",
        "Py_ReprLeave",
    }
)


def _collect_slot_map(source: str) -> dict[str, str]:
    slot_map: dict[str, str] = {}
    for slot, name in _SLOT_DESIGNATED_RE.findall(source):
        slot_map[name] = f"tp_{slot}"
    for slot, name in _SLOT_SPEC_RE.findall(source):
        slot_map[name] = f"tp_{slot}"
    return slot_map


def _slot_for(func_name: str, slot_map: dict[str, str]) -> str | None:
    """Classify a function as a recursion-prone slot, else None."""
    if func_name in slot_map:
        return slot_map[func_name]
    if _PARAM_WALK_RE.search(func_name):
        return "parameter_walk"
    for suffix, slot in _SLOT_NAME_SUFFIX:
        if func_name.endswith(suffix):
            return slot
    return None


def _has_guard(func: dict) -> bool:
    body = func["body"]
    return any(tok in body for tok in _GUARD_TOKENS)


def _has_loop(body_node) -> bool:
    for _ in walk_descendants(body_node, type_filter="for_statement"):
        return True
    for _ in walk_descendants(body_node, type_filter="while_statement"):
        return True
    for _ in walk_descendants(body_node, type_filter="do_statement"):
        return True
    return False


def _analyze_function(func: dict, slot: str, source_bytes: bytes, tree) -> dict | None:
    """Return a finding dict for an unguarded recursive slot, else None."""
    if _has_guard(func):
        return None

    calls = find_calls_in_scope(func["body_node"], source_bytes)
    call_names = {c["function_name"] for c in calls}

    self_recursive = func["name"] in call_names
    element_apis = call_names & _ELEMENT_RECURSION_APIS
    has_container = bool(_CONTAINER_ACCESS_RE.search(func["body"])) or _has_loop(
        func["body_node"]
    )
    element_descent = bool(element_apis) and has_container

    if not (self_recursive or element_descent):
        return None

    # Locate a representative line: the first self-call or element op.
    line = func["start_line"]
    for c in calls:
        if c["function_name"] == func["name"] or (
            c["function_name"] in _ELEMENT_RECURSION_APIS and element_descent
        ):
            line = c["start_line"]
            break

    if is_suppressed_by_comment(source_bytes, tree, line):
        return None

    if self_recursive:
        shape = "self_recursion"
        confidence = "high"
        why = (
            f"'{func['name']}' recurses into itself with no Py_EnterRecursiveCall guard"
        )
    else:
        shape = "container_element_descent"
        # hash/richcompare container descent is the confirmed SIGSEGV class;
        # repr/str often carry other bounds, so rate them a notch lower.
        confidence = "high" if slot in ("tp_hash", "tp_richcompare") else "medium"
        why = (
            f"'{func['name']}' ({slot}) descends into container elements via "
            f"{', '.join(sorted(element_apis))} with no Py_EnterRecursiveCall/"
            "Py_ReprEnter guard"
        )

    return {
        "type": "missing_recursion_guard",
        "function": func["name"],
        "slot": slot,
        "shape": shape,
        "line": line,
        "confidence": confidence,
        "detail": (
            f"{why} — a deeply-nested or cyclic object overflows the C stack "
            "(SIGSEGV) instead of raising RecursionError. Bracket the descent "
            "with Py_EnterRecursiveCall()/Py_LeaveRecursiveCall() (or "
            "Py_ReprEnter()/Py_ReprLeave() for repr/str)."
        ),
    }


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan for recursion-prone slots lacking a recursion guard."""
    project_root, scan_root = resolve_roots(target)

    findings: list[dict] = []
    total_functions = 0
    slot_functions = 0
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
        slot_map = _collect_slot_map(source)

        for func in functions:
            total_functions += 1
            slot = _slot_for(func["name"], slot_map)
            if slot is None:
                continue
            slot_functions += 1
            f = _analyze_function(func, slot, source_bytes, tree)
            if f is not None:
                f["file"] = rel
                findings.append(f)

    findings = deduplicate_findings(findings)

    by_confidence: dict[str, int] = defaultdict(int)
    by_shape: dict[str, int] = defaultdict(int)
    for f in findings:
        by_confidence[f["confidence"]] += 1
        by_shape[f["shape"]] += 1

    return build_report(
        project_root=project_root,
        scan_root=scan_root,
        files_analyzed=files_analyzed,
        functions_analyzed=total_functions,
        findings=findings,
        summary={
            "total_findings": len(findings),
            "by_confidence": dict(by_confidence),
            "by_shape": dict(by_shape),
        },
        recursion_prone_slot_functions=slot_functions,
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

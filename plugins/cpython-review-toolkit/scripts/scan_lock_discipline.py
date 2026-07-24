#!/usr/bin/env python3
"""Scan CPython C source for critical-section / lock-discipline issues.

CPython's own ``Objects/``, ``Modules/`` and ``Python/`` use per-object
critical sections pervasively for free-threading (PEP 703): dozens of
``Py_BEGIN_CRITICAL_SECTION(op)`` / ``Py_END_CRITICAL_SECTION()`` pairs live in
``Objects/dictobject.c`` alone. The macros are *scoped*: ``BEGIN`` opens a
brace and declares a stack-local ``PyCriticalSection`` that ``END`` pops. Leave
the section on any path without the matching ``END`` and the per-object lock is
never released — a deadlock the moment two threads contend the object.

This scanner focuses on the highest-signal shapes on CPython's own code:

* ``critical_section_missing_end`` / ``critical_section_end_on_error`` (FIX) —
  a ``Py_BEGIN_CRITICAL_SECTION`` (or the ``2`` / ``_MUTEX`` spelling) with no
  matching ``Py_END`` on some path, typically an early ``return`` or an
  out-of-section ``goto`` sitting between the begin and its end.
* ``mutex_leak_on_error`` / ``mutex_missing_unlock`` (FIX) — the same two
  shapes in the ``PyMutex`` family (``PyMutex_Lock`` / ``PyMutex_Unlock`` and
  the ``PyMutex``-backed ``LOCK_WEAKREFS`` macros). Strictly worse than the
  scoped-macro family: a ``PyMutex`` is *not* released by scope exit, so a
  leaked one stays locked for the process lifetime.
* ``nested_critical_sections`` (CONSIDER) — two *different* objects locked at
  once via two single-object begins instead of the deadlock-safe
  ``Py_BEGIN_CRITICAL_SECTION2(a, b)``.

The common, correct idiom (begin ... work ... end on every path) is silent.
Analysis is intra-function only: a section opened in one function and closed in
another is out of scope and honestly not modelled. A function that acquires a
lock and *never* releases it is likewise silent — that is CPython's deliberate
lock-helper convention (``extensions_lock_acquire``, ``_xidregistry_lock``),
the mirror image of the ``*_lock_held`` callee convention.

Statements the compiler can never reach are not lock leaks: an exit sitting
directly after an unconditional ``goto`` / ``return`` / ``break`` / ``continue``
in the same block is dead code and is skipped (``Objects/dictobject.c:4380``,
a leftover from the gh-112075 critical-section retrofit, is exactly this).

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
# with the ordinary ``Py_END_CRITICAL_SECTION()``. ``_MUTEX`` is absent from
# ``lock_macros.json`` (whose critical-section entry stays in sync with
# ft-review-toolkit), so it is spliced in here at the code level.
_CS_BEGIN_1 = "Py_BEGIN_CRITICAL_SECTION"
_CS_BEGIN_2 = "Py_BEGIN_CRITICAL_SECTION2"
_CS_MUTEX_BEGIN = "Py_BEGIN_CRITICAL_SECTION_MUTEX"
_CS_END_1 = "Py_END_CRITICAL_SECTION"
_CS_END_2 = "Py_END_CRITICAL_SECTION2"

# Lock families this scanner models, keyed by the ``type`` field of
# ``lock_macros.json``. Filtering the data file down to ``critical_section``
# alone (as this scanner used to) silently discarded the ``PyMutex`` pair the
# file already ships, which blinded it to ``Objects/weakrefobject.c``'s entire
# 16-site ``LOCK_WEAKREFS`` scheme and to every other ``PyMutex_Lock`` in the
# tree. Both families are loaded; each is paired independently so a
# ``PyMutex_Unlock`` can never be mistaken for a ``Py_END_CRITICAL_SECTION``.
_SCOPED_FAMILY = "critical_section"
_MUTEX_FAMILY = "python_mutex"
_MODELLED_FAMILIES = (_SCOPED_FAMILY, _MUTEX_FAMILY)

# Statement kinds that make whatever directly follows them in the same block
# unreachable. An exit sitting in that dead zone cannot leak a lock.
_UNCONDITIONAL_EXIT_TYPES = frozenset(
    {"goto_statement", "return_statement", "break_statement", "continue_statement"}
)

_lock_data: dict | None = None


def _load_lock_macros() -> dict:
    """Load the lock-macro vocabulary from data/."""
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


def _get_lock_families() -> dict[str, tuple[set[str], set[str]]]:
    """Return ``{family: (acquire_macros, release_macros)}`` from the data file.

    Every entry whose ``type`` is in :data:`_MODELLED_FAMILIES` contributes, so
    adding a macro pair to ``lock_macros.json`` is enough to widen coverage.
    """
    families: dict[str, tuple[set[str], set[str]]] = {
        name: (set(), set()) for name in _MODELLED_FAMILIES
    }
    for pair in _load_lock_macros().get("lock_pairs", []):
        family = pair.get("type")
        if family not in families:
            continue
        acquires, releases = families[family]
        acquires.update(pair.get("acquire", []))
        releases.update(pair.get("release", []))
    # Tolerate the mutex-backed begin CPython also uses (scoped semantics,
    # closed by the ordinary Py_END_CRITICAL_SECTION).
    families[_SCOPED_FAMILY][0].add(_CS_MUTEX_BEGIN)
    return {k: v for k, v in families.items() if v[0] and v[1]}


def _get_critical_section_pair() -> tuple[set[str], set[str]]:
    """Return ``(begin_macros, end_macros)`` for the critical-section family."""
    return _get_lock_families().get(_SCOPED_FAMILY, (set(), set()))


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


def _is_unreachable_statement(node) -> bool:
    """True if ``node`` sits directly after an unconditional jump in its block.

    ``goto slow_exit; return -1;`` — the ``return`` is dead code the compiler
    can never reach, so it cannot leak a lock. This is the exact shape of
    ``Objects/dictobject.c:4380``, a leftover from the gh-112075 retrofit that
    was the scanner's only ``Objects/`` finding and a false positive.
    """
    parent = node.parent
    if parent is None or parent.type != "compound_statement":
        return False
    previous = None
    for child in parent.named_children:
        if child.id == node.id:
            break
        if child.type == "comment":
            continue
        previous = child
    return previous is not None and previous.type in _UNCONDITIONAL_EXIT_TYPES


def _collect_exits(func_node, source_bytes: bytes) -> list[dict]:
    """Collect every reachable ``return`` and ``goto`` (byte-ordered)."""
    exits: list[dict] = []
    for ret in find_return_statements(func_node, source_bytes):
        if _is_unreachable_statement(ret["node"]):
            continue
        exits.append(
            {
                "kind": "return",
                "byte": ret["node"].start_byte,
                "line": ret["start_line"],
                "label": None,
                "node": ret["node"],
            }
        )
    for node in walk_descendants(func_node, "goto_statement"):
        if _is_unreachable_statement(node):
            continue
        label_node = node.child_by_field_name("label")
        exits.append(
            {
                "kind": "goto",
                "byte": node.start_byte,
                "line": node.start_point[0] + 1,
                "label": get_node_text(label_node, source_bytes)
                if label_node is not None
                else None,
                "node": node,
            }
        )
    return exits


def _enclosing_block(node):
    """Innermost ``compound_statement`` containing ``node`` (None at top level)."""
    parent = node.parent
    while parent is not None and parent.type != "compound_statement":
        parent = parent.parent
    return parent


def _block_chain(exit_node, body_node) -> list:
    """``(block, child)`` pairs from ``exit_node`` up to the function body.

    Every statement that textually precedes ``child`` *in ``block``* is
    guaranteed to execute before ``exit_node`` on the path that reaches it —
    a cheap textual dominator, exact enough for lock bookkeeping and immune to
    the sibling-branch confusion a flat "is it between these two bytes" test
    has.
    """
    chain: list = []
    child = exit_node
    parent = child.parent
    while parent is not None:
        if parent.type == "compound_statement":
            chain.append((parent, child))
        if parent.id == body_node.id:
            break
        child = parent
        parent = parent.parent
    return chain


def _dominates(node, chain: list, after_byte: int = -1) -> bool:
    """True if ``node`` executes before the exit that produced ``chain``."""
    for block, child in chain:
        if not after_byte < node.start_byte < child.start_byte:
            continue
        enclosing = _enclosing_block(node)
        if enclosing is not None and enclosing.id == block.id:
            return True
    return False


def _pair_events(events: list[dict], *, match_args: bool) -> None:
    """Pair acquires with releases in place, filling each acquire's ``end_byte``.

    ``match_args`` picks the innermost *open* acquire whose argument text equals
    the release's — the ``PyMutex`` family names the same mutex on both sides,
    so this is exact where plain LIFO would mispair interleaved locks. The
    scoped family's ``Py_END_CRITICAL_SECTION()`` takes no arguments, so it
    pairs LIFO.
    """
    stack: list[dict] = []
    for ev in events:
        if ev["is_begin"]:
            stack.append(ev)
            continue
        if not stack:
            continue
        index = len(stack) - 1
        if match_args and ev["args"]:
            wanted = _norm_args(ev["args"])
            for i in range(len(stack) - 1, -1, -1):
                if _norm_args(stack[i]["args"]) == wanted:
                    index = i
                    break
        stack[index]["end_byte"] = ev["byte"]
        stack.pop(index)


def _releases_after(releases: list[dict], label_byte: int, begin: dict) -> bool:
    """True if a release of the same lock appears at or after ``label_byte``.

    Models the `goto cleanup` ladder: the jump leaves the section textually,
    but the label block releases the lock before returning. Where both sides
    name an argument (the PyMutex family) the argument text must match, so a
    cleanup block unlocking a *different* mutex does not launder the finding.
    """
    wanted = _norm_args(begin["args"])
    for release in releases:
        if release["byte"] < label_byte:
            continue
        if wanted and release["args"] and _norm_args(release["args"]) != wanted:
            continue
        return True
    return False


def _analyze_lock_family(
    func: dict,
    source_bytes: bytes,
    family: str,
    acquires_set: set[str],
    releases_set: set[str],
    labels: dict[str, int],
    exits: list[dict],
) -> list[dict]:
    """Flag missing / early-exited / nested locks of one family in one function."""
    calls = find_calls_in_scope(
        func["body_node"], source_bytes, acquires_set | releases_set
    )
    if not calls:
        return []

    events = sorted(
        (
            {
                "name": c["function_name"],
                "line": c["start_line"],
                "byte": c["start_byte"],
                "args": c["arguments_text"],
                "is_begin": c["function_name"] in acquires_set,
                "end_byte": None,
                "node": c["node"],
            }
            for c in calls
        ),
        key=lambda e: e["byte"],
    )
    scoped = family == _SCOPED_FAMILY
    findings: list[dict] = []

    # Two *different* objects held at once via two single-object begins is a
    # lock-order hazard whichever family they come from.
    if scoped:
        open_stack: list[dict] = []
        for ev in events:
            if ev["is_begin"]:
                if open_stack:
                    outer = open_stack[-1]
                    if (
                        ev["name"] != _CS_BEGIN_2
                        and outer["name"] != _CS_BEGIN_2
                        and _norm_args(ev["args"]) != _norm_args(outer["args"])
                        and outer["args"]
                        and ev["args"]
                    ):
                        findings.append(_nested_finding(func, outer, ev))
                open_stack.append(ev)
            elif open_stack:
                open_stack.pop()

    _pair_events(events, match_args=not scoped)

    # An acquire never released *anywhere* in the function is CPython's
    # deliberate lock-helper convention (extensions_lock_acquire,
    # _PyCriticalSection_BeginSlow, stop_the_world) — the release is the
    # caller's job, exactly like the `*_lock_held` callee convention. Only a
    # *partially* paired function can be leaking on one path.
    releases_present = any(not ev["is_begin"] for ev in events)

    ended: list[dict] = []
    for ev in events:
        if not ev["is_begin"]:
            continue
        if ev["end_byte"] is None:
            if scoped or releases_present:
                findings.append(_missing_end_finding(func, ev, family))
        else:
            ended.append(ev)

    # A return/goto strictly between an acquire and its matching release leaves
    # the lock held on that path. Two shapes of goto do not:
    #   * the target label is itself inside the locked region (a `retry:` loop)
    #     — an internal jump, not an exit;
    #   * the target label is a cleanup block that releases the lock again
    #     (`goto error; ... error: PyMutex_Unlock(&self->mutex);`) — the
    #     canonical release-then-exit ladder, with the release duplicated at the
    #     label rather than fallen through to.
    releases = [ev for ev in events if not ev["is_begin"]]
    if not scoped:
        # A PyMutex is not scope-bound, so "between the acquire and its matched
        # release" is the wrong window: the same mutex is routinely released on
        # several branches. Use textual dominance instead — the acquire must
        # execute before the exit, and no release may execute between them.
        findings.extend(
            _mutex_leaking_exits(func, family, events, releases, labels, exits)
        )
        return findings
    for begin in ended:
        e_byte = begin["end_byte"]
        for exit_ in exits:
            if not begin["byte"] < exit_["byte"] < e_byte:
                continue
            if exit_["kind"] == "goto":
                target = labels.get(exit_["label"] or "")
                if target is not None and begin["byte"] < target < e_byte:
                    continue
                if target is not None and _releases_after(releases, target, begin):
                    continue
            findings.append(_end_on_error_finding(func, begin, exit_, family))

    return findings


def _mutex_leaking_exits(
    func: dict,
    family: str,
    events: list[dict],
    releases: list[dict],
    labels: dict[str, int],
    exits: list[dict],
) -> list[dict]:
    """Exits reached with a ``PyMutex``-family lock still held."""
    if not releases:
        # No release anywhere: the deliberate lock-helper convention.
        return []
    body_node = func["body_node"]
    acquires = [ev for ev in events if ev["is_begin"]]
    findings: list[dict] = []
    for exit_ in exits:
        chain = _block_chain(exit_["node"], body_node)
        if not chain:
            continue
        for acquire in acquires:
            if not _dominates(acquire["node"], chain):
                continue
            wanted = _norm_args(acquire["args"])
            released = any(
                _dominates(r["node"], chain, acquire["byte"])
                and not (wanted and r["args"] and _norm_args(r["args"]) != wanted)
                for r in releases
            )
            if released:
                continue
            if exit_["kind"] == "goto":
                target = labels.get(exit_["label"] or "")
                if target is not None and _releases_after(releases, target, acquire):
                    continue
            findings.append(_end_on_error_finding(func, acquire, exit_, family))
            break
    return findings


def _analyze_critical_sections(func: dict, source_bytes: bytes) -> list[dict]:
    """Flag missing / early-exited / nested locks in one function, all families."""
    families = _get_lock_families()
    if not families:
        return []
    body = func["body"]
    # Cheap pre-filter: skip the tree walk unless some vocabulary appears.
    relevant = {
        name: pair
        for name, pair in families.items()
        if any(tok in body for tok in pair[0] | pair[1])
    }
    if not relevant:
        return []

    labels = _collect_labels(func["body_node"], source_bytes)
    exits = _collect_exits(func["body_node"], source_bytes)

    findings: list[dict] = []
    for name, (acquires, releases) in sorted(relevant.items()):
        findings.extend(
            _analyze_lock_family(
                func, source_bytes, name, acquires, releases, labels, exits
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Finding builders
# ---------------------------------------------------------------------------


_MUTEX_SEVERITY_NOTE = (
    "A PyMutex is not released by scope exit the way Py_END_CRITICAL_SECTION is, "
    "so a leaked one stays locked for the lifetime of the process"
)


def _missing_end_finding(func: dict, begin: dict, family: str = _SCOPED_FAMILY) -> dict:
    if family == _MUTEX_FAMILY:
        return {
            "type": "mutex_missing_unlock",
            "function": func["name"],
            "line": begin["line"],
            "classification": "FIX",
            "confidence": "medium",
            "detail": (
                f"{begin['name']}({begin['args']}) at line {begin['line']} in "
                f"'{func['name']}' is never released on this path, although the "
                f"function does release the lock elsewhere — so this is a partially "
                f"paired acquire, not the deliberate lock-helper convention. "
                f"{_MUTEX_SEVERITY_NOTE}."
            ),
        }
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


def _end_on_error_finding(
    func: dict, begin: dict, exit_: dict, family: str = _SCOPED_FAMILY
) -> dict:
    where = (
        f"return at line {exit_['line']}"
        if exit_["kind"] == "return"
        else f"goto {exit_['label']} at line {exit_['line']}"
    )
    if family == _MUTEX_FAMILY:
        release = begin["name"].replace("LOCK", "UNLOCK", 1)
        if begin["name"] == "PyMutex_Lock":
            release = "PyMutex_Unlock"
        return {
            "type": "mutex_leak_on_error",
            "function": func["name"],
            "line": exit_["line"],
            "classification": "FIX",
            "confidence": "high",
            "detail": (
                f"{where} in '{func['name']}' leaves the lock taken by "
                f"{begin['name']}({begin['args']}) at line {begin['line']} held — "
                f"call {release}({begin['args']}) before the exit. "
                f"{_MUTEX_SEVERITY_NOTE}."
            ),
        }
    end = _matching_end(begin["name"])
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


def _has_mutex_lock(func: dict) -> bool:
    """Cheap check: does the function mention any modelled PyMutex acquire?"""
    acquires = _get_lock_families().get(_MUTEX_FAMILY, (set(), set()))[0]
    return any(tok in func["body"] for tok in acquires)


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan for critical-section / lock-discipline issues in CPython C source."""
    project_root, scan_root = resolve_roots(target)

    findings: list[dict] = []
    files_analyzed = 0
    total_functions = 0
    cs_functions = 0
    mutex_functions = 0
    skipped: list[dict] = []
    # Raw vocabulary counts give a *denominator* for a zero result: "no
    # constructs present" and "constructs present and all clean" are different
    # answers, and a bare 0 cannot tell them apart.
    families = _get_lock_families()
    vocabulary = sorted(
        {tok for acquires, releases in families.values() for tok in acquires | releases}
    )
    vocab_counts: dict[str, int] = dict.fromkeys(vocabulary, 0)

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

        source = source_bytes.decode("utf-8", errors="replace")
        for token in vocabulary:
            if token in source:
                vocab_counts[token] += len(
                    re.findall(rf"\b{re.escape(token)}\s*\(", source)
                )

        functions = extract_functions(tree, source_bytes)
        if not functions:
            continue

        files_analyzed += 1
        rel = relpath(filepath, project_root)

        for func in functions:
            total_functions += 1
            if _has_critical_section(func):
                cs_functions += 1
            if _has_mutex_lock(func):
                mutex_functions += 1
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
        mutex_functions=mutex_functions,
        lock_families=sorted(families),
        vocabulary_counts={k: v for k, v in sorted(vocab_counts.items()) if v},
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

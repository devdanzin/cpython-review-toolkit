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

  The ``Py_SETREF(x->field, NULL)`` spelling of that drop gets its own type,
  ``iternext_setref_null_decref``: unlike ``Py_CLEAR`` the ``Py_SETREF`` family
  has **no internal NULL guard**, so the loser of the race evaluates
  ``Py_DECREF(NULL)`` and the failure mode escalates from a double-DECREF to an
  immediate SIGSEGV. Reproduced under ASan on a free-threaded build at
  ``Objects/genericaliasobject.c:952`` (``ga_iternext``).

* **T2 — lazy-init cache without a critical section**: ``if (!self->f) self->f =
  compute();`` with no critical section in the function — two threads both see
  NULL, both compute and store (leak + torn/lost write). Confirmed:
  ``descr_get_qualname`` (Objects/descrobject.c, TSAN-0043).

  A bare lazy init stays *medium* confidence, because in isolation it is often
  a single-threaded init path. The discriminator that earns high confidence is
  the **guarded twin**: when a file has ≥2 accessors of the same field in the
  lazy-init shape and at least one of them *is* guarded, the guard is proof the
  maintainers already agreed the field needs protection — and a critical
  section held by only one of two accessors serialises nothing. That case is
  emitted as ``lazy_init_partial_guard``. It fires on
  ``Objects/genericaliasobject.c:583`` (``ga_getitem``), whose twin
  ``ga_parameters_lock_held`` was wrapped by gh-153298 seven days before HEAD
  while ``ga_getitem``'s identical inline init of the *same field* was left
  alone; TSan reproduces three races there.

* **T1 — atomic/plain access asymmetry**: a struct field accessed via an atomic
  macro (``_Py_atomic_*`` / ``FT_ATOMIC_*``) at one site and as a plain
  ``x->field`` load/store at another in the same file. The asymmetry means one
  access is unsynchronized. Confirmed: itertools ``count_repr`` (TSAN-0006).

Free-threading preprocessor regions are modelled throughout. A plain access in
the GIL-only arm of an ``#ifdef Py_GIL_DISABLED`` split is unreachable on the
free-threaded build and is never a race; conversely, a drop *elided* under
``#ifndef Py_GIL_DISABLED`` is a **fixed** T3 — that is the second canonical fix
strategy in ``Objects/`` (``tupleiter_next``, ``listiter_next``,
``reversed_next``) and its presence in a file is guarded-twin evidence.

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

# iternext slot wiring. CPython's own Objects/ overwhelmingly uses the
# *positional* static PyTypeObject form with a trailing /* tp_iternext */
# comment (tupleiter_next, listiter_next, striter_next, ...), so matching only
# the designated-initializer and PyType_Slot spellings misses most of the tree's
# iterators — and with them the whole "elide the drop under FT" fix strategy.
_ITERNEXT_DESIGNATED_RE = re.compile(
    r"\.tp_iternext\s*=\s*(?:\(\s*\w[\w\s\*]*\)\s*)?(?:&\s*)?(\w+)"
)
_ITERNEXT_SPEC_RE = re.compile(r"\{\s*Py_tp_iternext\s*,\s*(?:&\s*)?(\w+)\s*\}")
_ITERNEXT_POSITIONAL_RE = re.compile(
    r"^[ \t]*(?:\(\s*\w[\w\s\*]*\)\s*)?(?:&\s*)?([A-Za-z_]\w*)\s*,"
    r"[ \t]*/\*\s*tp_iternext\s*\*/",
    re.MULTILINE,
)

# T3: dropping an owning ref to a self-member.
_MEMBER_CLEAR_RE = re.compile(r"Py_CLEAR\s*\(\s*\w+\s*->\s*(\w+)\s*\)")
_MEMBER_SET_NULL_RE = re.compile(r"\b\w+\s*->\s*(\w+)\s*=\s*NULL\b")
_DECREF_RE = re.compile(r"\bPy_X?DECREF\s*\(")
# Py_SETREF / Py_XSETREF have no internal NULL guard, unlike Py_CLEAR.
_MEMBER_SETREF_NULL_RE = re.compile(
    r"\bPy_X?SETREF\s*\(\s*\w+\s*->\s*(\w+)\s*,\s*NULL\s*\)"
)

# T2: lazy-init of a self-member guarded only by a NULL check.
_LAZY_INIT_RE = re.compile(
    r"if\s*\(\s*(?:!\s*)?(\w+)\s*->\s*(\w+)\s*(?:==\s*NULL\s*)?\)\s*"
    r"(?:\{)?[^;{}]*?\1\s*->\s*\2\s*=\s*(?!=)",
    re.DOTALL,
)

# Free-threading preprocessor conditionals.
_PREPROC_RE = re.compile(r"^[ \t]*#[ \t]*(if|ifdef|ifndef|elif|else|endif)\b(.*)$")
_GIL_DISABLED_TOKEN = "Py_GIL_DISABLED"

# Constructors / initializers: a plain write to a freshly-allocated object that
# no other thread can reach yet is not a race (Objects/weakrefobject.c:65
# `init_weakref`). Names only — the write must also be pre-publication, which
# is what these names encode by convention.
_INITIALIZER_NAME_RE = re.compile(
    r"(?:^|_)(?:init|new|alloc|create|make)(?:_|$)|_New$|^new_|^init_", re.IGNORECASE
)


# Comments and literals in one left-to-right pass. String and char literals are
# matched only so a `//` inside `"http://..."` or a quote inside `'"'` cannot be
# mistaken for a comment opener; their text is left alone.
_MASK_RE = re.compile(
    r"(/\*.*?\*/|//[^\n]*)"  # group 1: comments — blanked
    r'|"(?:[^"\\\n]|\\.)*"'  # string literal — kept
    r"|'(?:[^'\\\n]|\\.)*'",  # character literal — kept
    re.DOTALL,
)


def _mask(match: re.Match) -> str:
    if match.group(1) is None:
        return match.group(0)
    text = match.group(0)
    newlines = text.count("\n")
    return " " * (len(text) - newlines) + "\n" * newlines


def strip_comments(source: str) -> str:
    """Blank out C comments while preserving every newline and byte offset.

    Line-number fidelity is the whole point: the shared
    ``tree_sitter_utils.strip_comments`` collapses a block comment to a single
    space, which shifts every subsequent line. Prose in a comment is also a
    real false-positive source here — ``Objects/weakrefobject.c:112`` reads
    ``// self->wr_object may be Py_None ...``, which the plain-member regex
    happily matched as an unsynchronised field access.
    """
    return _MASK_RE.sub(_mask, source)


def _has_lock(body: str) -> bool:
    return any(tok in body for tok in _LOCK_TOKENS)


def _caller_holds_lock(func_name: str) -> bool:
    """CPython FT convention: a ``*_lock_held`` function runs with the critical
    section already held by its caller, so the lock is not missing here."""
    return func_name.endswith(("_lock_held", "_locked", "_LockHeld"))


def _collect_iternext_names(source: str) -> set[str]:
    names = set(_ITERNEXT_DESIGNATED_RE.findall(source))
    names |= set(_ITERNEXT_SPEC_RE.findall(source))
    names |= set(_ITERNEXT_POSITIONAL_RE.findall(source))
    names.discard("0")
    return names


def _is_iternext(func_name: str, slot_names: set[str]) -> bool:
    return func_name in slot_names or "iternext" in func_name


# ---------------------------------------------------------------------------
# Free-threading preprocessor regions
# ---------------------------------------------------------------------------


def _gil_disabled_regions(
    source: str,
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Split the file into GIL-only and free-threaded-only line ranges.

    Returns ``(gil_only, ft_only)`` as lists of inclusive 1-indexed
    ``(start, end)`` line ranges.

    * *GIL-only* = the arm compiled when ``Py_GIL_DISABLED`` is **un**defined:
      ``#ifndef Py_GIL_DISABLED`` ... and the ``#else`` of
      ``#ifdef Py_GIL_DISABLED``. Nothing there can race — the free-threaded
      build never compiles it.
    * *FT-only* = the mirror image. A drop or an atomic that exists only here is
      evidence of an already-applied free-threading fix.

    A plain line scan rather than a tree-sitter walk: CPython's conditionals
    routinely split struct initializers and if/else chains, which is precisely
    where the C grammar gives up and emits ERROR nodes.
    """
    gil_only: list[tuple[int, int]] = []
    ft_only: list[tuple[int, int]] = []
    # Stack entries: [kind, open_line]; kind is "gil_only" / "ft_only" / None.
    stack: list[list] = []

    def _close(entry: list, end_line: int) -> None:
        kind, start = entry[0], entry[1]
        if kind is None or end_line <= start:
            return
        (gil_only if kind == "gil_only" else ft_only).append((start + 1, end_line - 1))

    for lineno, line in enumerate(source.split("\n"), start=1):
        m = _PREPROC_RE.match(line)
        if m is None:
            continue
        directive, rest = m.group(1), m.group(2)
        if directive in ("if", "ifdef", "ifndef"):
            kind = None
            if _GIL_DISABLED_TOKEN in rest:
                if directive == "ifndef":
                    kind = "gil_only"
                elif directive == "ifdef":
                    kind = "ft_only"
                else:  # plain #if
                    negated = "!" in rest.split(_GIL_DISABLED_TOKEN, 1)[0]
                    kind = "gil_only" if negated else "ft_only"
            stack.append([kind, lineno])
        elif directive in ("else", "elif"):
            if not stack:
                continue
            entry = stack[-1]
            _close(entry, lineno)
            if directive == "else" and entry[0] is not None:
                entry[0] = "ft_only" if entry[0] == "gil_only" else "gil_only"
            else:
                entry[0] = None
            entry[1] = lineno
        elif directive == "endif":
            if not stack:
                continue
            _close(stack.pop(), lineno)

    # Unterminated conditionals (truncated file): close at EOF.
    eof = source.count("\n") + 2
    while stack:
        _close(stack.pop(), eof)
    return gil_only, ft_only


def _in_ranges(line: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= line <= end for start, end in ranges)


def _line_of(body: str, match_start: int, body_start_line: int) -> int:
    """1-indexed source line of a match inside a function body.

    ``body_start_line`` must be the line of the body's opening brace, not the
    line the ``function_definition`` node starts on: CPython puts the return
    type on its own line above the signature, so using the node start reports
    every finding two lines early.
    """
    return body_start_line + body[:match_start].count("\n")


def _body_start_line(func: dict) -> int:
    body_node = func.get("body_node")
    if body_node is not None:
        return body_node.start_point[0] + 1
    return func["start_line"]


def _function_at_line(functions: list[dict], line: int) -> dict | None:
    for func in functions:
        if func["start_line"] <= line <= func["end_line"]:
            return func
    return None


def _check_t3(
    func: dict,
    is_iter: bool,
    tree,
    source_bytes: bytes,
    gil_only: list[tuple[int, int]],
) -> dict | None:
    """T3: iternext that drops an owning self-member ref without a lock."""
    if not is_iter or _caller_holds_lock(func["name"]):
        return None
    body = strip_comments(func["body"])
    if _has_lock(body):
        return None
    base = _body_start_line(func)

    # Py_SETREF/Py_XSETREF first: same bug, strictly worse failure mode, and
    # the two spellings can coexist in one function.
    for match, kind in _iter_drop_candidates(body):
        line = _line_of(body, match.start(), base)
        if _in_ranges(line, gil_only):
            # The drop is elided on the free-threaded build — this is the
            # tupleiter/listiter/reversed fix strategy, already applied.
            continue
        if is_suppressed_by_comment(source_bytes, tree, line):
            continue
        member = match.group(1)
        if kind == "setref":
            return {
                "type": "iternext_setref_null_decref",
                "ft_class": "T3",
                "function": func["name"],
                "member": member,
                "line": line,
                "confidence": "high",
                "detail": (
                    f"{func['name']} is a tp_iternext that drops its owning "
                    f"reference to '{member}' with Py_SETREF(..., NULL) and no "
                    "Py_BEGIN_CRITICAL_SECTION. Unlike Py_CLEAR, the Py_SETREF "
                    "family has no internal NULL guard: a thread that passes "
                    "the NULL check before another thread's store completes "
                    "evaluates Py_DECREF(NULL), so the failure mode is an "
                    "immediate SIGSEGV rather than a double-DECREF (reproduced "
                    "under ASan at Objects/genericaliasobject.c:952, "
                    "ga_iternext). Wrap the body in "
                    "Py_BEGIN_CRITICAL_SECTION(self); switching to Py_CLEAR "
                    "only downgrades the crash, it does not fix the race."
                ),
            }
        return {
            "type": "iternext_double_decref",
            "ft_class": "T3",
            "function": func["name"],
            "member": member,
            "line": line,
            "confidence": "high",
            "detail": (
                f"{func['name']} is a tp_iternext that drops an owning "
                f"reference to the shared self-member '{member}' (Py_CLEAR / "
                "member=NULL + Py_DECREF) with no Py_BEGIN_CRITICAL_SECTION. "
                "Under the free-threaded build two concurrent next() calls read "
                "the same borrowed sequence and both DECREF it -> double-free "
                "(cf. gh-154130 dict, gh-144357 set, gh-153296 StringIO). Wrap "
                "the iternext body in Py_BEGIN_CRITICAL_SECTION(self), or elide "
                "the drop under #ifndef Py_GIL_DISABLED the way tupleiter_next "
                "/ listiter_next / reversed_next do."
            ),
        }
    return None


def _iter_drop_candidates(body: str):
    """Yield ``(match, kind)`` for every owning-ref drop of a self-member."""
    candidates = [(m, "setref") for m in _MEMBER_SETREF_NULL_RE.finditer(body)]
    candidates += [(m, "clear") for m in _MEMBER_CLEAR_RE.finditer(body)]
    if _DECREF_RE.search(body):
        candidates += [(m, "setnull") for m in _MEMBER_SET_NULL_RE.finditer(body)]
    # setref first (worst failure mode), then source order.
    candidates.sort(key=lambda pair: (pair[1] != "setref", pair[0].start()))
    return candidates


def _collect_lazy_init_sites(
    func: dict, source: str, gil_only: list[tuple[int, int]]
) -> list[dict]:
    """Every lazy-init-of-a-self-member site in one function, guarded or not.

    Guardedness is recorded rather than used to suppress, because a *guarded*
    accessor is the evidence that makes an unguarded sibling a high-confidence
    finding.
    """
    if _TYPE_CONSTRUCTION_FN_RE.search(func["name"]):
        return []
    body = strip_comments(func["body"])
    base = _body_start_line(func)
    guarded = _has_lock(body) or _caller_holds_lock(func["name"])
    sites: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for m in _LAZY_INIT_RE.finditer(body):
        obj, field = m.group(1), m.group(2)
        if (obj, field) in seen:
            continue
        seen.add((obj, field))
        line = _line_of(body, m.start(), base)
        if _in_ranges(line, gil_only):
            continue
        if _is_stack_local(body, obj) or _points_at_stack_aggregate(func, source, obj):
            # `unionbuilder ub;` — a function-local aggregate is never shared,
            # so `if (ub->f == NULL) ub->f = ...` cannot race
            # (Objects/unionobject.c:173).
            continue
        sites.append(
            {
                "function": func["name"],
                "object": obj,
                "field": field,
                "line": line,
                "guarded": guarded,
            }
        )
    return sites


def _is_stack_local(body: str, var: str) -> bool:
    """True if ``var`` is declared in this body as a by-value local aggregate."""
    pattern = rf"(?:^|[;{{}}]|\)\s)\s*(?!return\b)[A-Za-z_]\w*\s+{re.escape(var)}\s*(?:=|;|\[)"
    return re.search(pattern, body) is not None


def _param_type_of(func: dict, var: str) -> str | None:
    """Declared type of pointer parameter ``var``, e.g. ``unionbuilder *ub``."""
    m = re.search(
        rf"\b([A-Za-z_]\w*)\s*\*\s*{re.escape(var)}\s*(?:,|$|\))",
        func.get("parameters", ""),
    )
    return m.group(1) if m else None


def _points_at_stack_aggregate(func: dict, source: str, var: str) -> bool:
    """True if ``var`` is a ``T *`` parameter and the file declares ``T`` by value.

    ``unionbuilder_add_single_unchecked(unionbuilder *ub, ...)`` mutates a
    caller-owned *stack* aggregate (``unionbuilder ub;`` at
    Objects/unionobject.c:264/400/487), never a shared heap object, so its
    ``if (ub->f == NULL) ub->f = ...`` cannot race. Real ``PyObject`` subtypes
    are never declared by value, so this does not launder heap state.
    """
    type_name = _param_type_of(func, var)
    if not type_name:
        return False
    return (
        re.search(rf"(?:^|[;{{}}\n])\s*{re.escape(type_name)}\s+\w+\s*;", source)
        is not None
    )


_ALLOCATOR_CALL_RE = (
    r"(?:_?Py\w*(?:_New|_NEW|_Malloc|_MALLOC|_Alloc|_ALLOC)\w*"
    r"|_Py_FREELIST_POP|malloc|calloc)\s*\("
)


def _is_freshly_allocated(body: str, var: str, before: int) -> bool:
    """True if ``var`` was assigned from an allocator earlier in this body.

    A store into an object the current thread just allocated and has not yet
    published is unreachable by any other thread — ``tuple_iter``'s
    ``it = PyObject_GC_New(...); it->it_index = 0;`` is not a race.
    """
    pattern = rf"\b{re.escape(var)}\s*=\s*(?:\([^;)]*\)\s*)?{_ALLOCATOR_CALL_RE}"
    for m in re.finditer(pattern, body):
        if m.start() < before:
            return True
    return False


def _build_t2_findings(sites: list[dict], tree, source_bytes: bytes) -> list[dict]:
    """Turn per-file lazy-init sites into T2 findings.

    A field with ≥2 accessors where ≥1 is guarded and ≥1 is not gets
    ``lazy_init_partial_guard`` at high confidence: the guard proves the field
    needs protection, and a critical section held by only one of two accessors
    serialises nothing. Everything else stays the medium-confidence bare shape.
    """
    by_field: dict[str, list[dict]] = defaultdict(list)
    for site in sites:
        by_field[site["field"]].append(site)

    findings: list[dict] = []
    for field, group in by_field.items():
        guarded_twins = [s for s in group if s["guarded"]]
        for site in group:
            if site["guarded"]:
                continue
            line = site["line"]
            if is_suppressed_by_comment(source_bytes, tree, line):
                continue
            member = f"{site['object']}->{field}"
            if guarded_twins:
                twin = guarded_twins[0]
                findings.append(
                    {
                        "type": "lazy_init_partial_guard",
                        "ft_class": "T2",
                        "function": site["function"],
                        "member": member,
                        "line": line,
                        "confidence": "high",
                        "guarded_twin": f"{twin['function']}:{twin['line']}",
                        "detail": (
                            f"{site['function']} lazily initialises "
                            f"'{member}' with no critical section, but "
                            f"{twin['function']} (line {twin['line']}) "
                            "initialises the same field under one. A critical "
                            "section held by only some accessors of a field "
                            "serialises nothing: the guarded and unguarded "
                            "accessors race each other, and the unguarded one "
                            "races itself. The guarded twin is proof the field "
                            "needs protection and is the fix to copy — route "
                            f"{site['function']} through the same lock-held "
                            "helper (cf. Objects/unionobject.c "
                            "union_init_parameters, one helper called by both "
                            "accessors, vs Objects/genericaliasobject.c where "
                            "gh-153298 guarded only one of two)."
                        ),
                    }
                )
            else:
                findings.append(
                    {
                        "type": "lazy_init_no_critical_section",
                        "ft_class": "T2",
                        "function": site["function"],
                        "member": member,
                        "line": line,
                        "confidence": "medium",
                        "detail": (
                            f"Lazy init of '{member}' guarded only by a NULL "
                            "check, with no critical section. Under "
                            "free-threading two threads both observe NULL and "
                            "both compute/store -> leak + torn/lost write (cf. "
                            "descr_get_qualname / TSAN-0043). Guard with "
                            "Py_BEGIN_CRITICAL_SECTION or a compare-exchange."
                        ),
                    }
                )
    return findings


# Calls whose arguments are not concurrent accesses: `assert` disappears under
# NDEBUG, and the printf family only appears in debug dumps and fatal paths.
_DEBUG_ONLY_CALLS = (
    "assert",
    "_PyObject_ASSERT",
    "_PyObject_ASSERT_WITH_MSG",
    "_PyObject_ASSERT_FROM",
    "printf",
    "fprintf",
    "PySys_WriteStderr",
    "PySys_FormatStderr",
)
# Functions that only ever run single-threaded: debug dumpers and the
# consistency checkers CPython calls from asserts and fatal-error paths.
_DEBUG_ONLY_FN_RE = re.compile(
    r"CheckConsistency$|^dump_|_dump$|^_?Py\w*DebugDump|^print_", re.IGNORECASE
)

# `PyType_Ready` runs to completion before the type object is reachable from
# Python, so the slot-inheritance helpers it calls populate fields nothing else
# can observe yet. Their `if (type->tp_x == NULL) type->tp_x = base->tp_x;`
# lines match the lazy-init shape but are pure construction.
_TYPE_CONSTRUCTION_FN_RE = re.compile(r"^type_ready|^inherit_|_ready$")


def _balanced_call_span(source: str, start: int) -> tuple[int, int]:
    """Byte span of the call whose name starts at ``start``, parens included."""
    open_paren = source.find("(", start)
    if open_paren == -1:
        return (start, start + 1)
    depth = 0
    for i in range(open_paren, len(source)):
        ch = source[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return (start, i + 1)
    return (start, len(source))


def _call_spans(source: str, names: tuple[str, ...]) -> list[tuple[int, int]]:
    """Byte spans of every call to one of ``names`` (arguments included)."""
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\s*\(")
    return [_balanced_call_span(source, m.start()) for m in pattern.finditer(source)]


def _check_t1(
    source: str,
    rel: str,
    functions: list[dict],
    gil_only: list[tuple[int, int]],
) -> list[dict]:
    """T1: a field accessed atomically at one site and plainly at another."""
    # Prose in a comment is not a field access (Objects/weakrefobject.c:112).
    source = strip_comments(source)
    atomic_fields = set(_ATOMIC_ACCESS_RE.findall(source))
    if not atomic_fields:
        return []

    # Byte ranges covered by atomic-macro calls, so plain accesses inside an
    # atomic call are not counted as "plain". The span must cover the *whole*
    # call, not just up to the first field name: the second argument of
    # `_Py_atomic_store_ssize_release(&keys->dk_usable, keys->dk_usable - 1)`
    # is not an unsynchronised access (Objects/dictobject.c:249).
    atomic_spans = [
        _balanced_call_span(source, m.start())
        for m in _ATOMIC_ACCESS_RE.finditer(source)
    ]
    # Debug-only readers: `assert()` compiles out with NDEBUG, and the dump /
    # consistency-check helpers run single-threaded from a debugger or a fatal
    # path (Objects/dictobject.c dump_entries, _PyDict_CheckConsistency).
    debug_spans = _call_spans(source, _DEBUG_ONLY_CALLS)
    exempt = sorted(atomic_spans + debug_spans)

    def _in_atomic(pos: int) -> bool:
        return any(s <= pos < e for s, e in exempt)

    findings: list[dict] = []
    reported: set[str] = set()
    for m in _PLAIN_MEMBER_RE.finditer(source):
        field = m.group(2)
        if field not in atomic_fields or field in reported:
            continue
        if _in_atomic(m.start()):
            continue
        line = source[: m.start()].count("\n") + 1
        if _in_ranges(line, gil_only):
            # The GIL-only arm of an #ifdef Py_GIL_DISABLED split: the
            # free-threaded build never compiles it, so it cannot race
            # (Objects/tupleobject.c:1165, tupleiter_len).
            continue
        owner = _function_at_line(functions, line)
        if owner is not None:
            if _DEBUG_ONLY_FN_RE.search(owner["name"]):
                continue
            if _caller_holds_lock(owner["name"]) or _has_lock(owner["body"]):
                # The plain access runs under a lock; T1's whole premise is
                # that *nothing* synchronises it.
                continue
            tail = source[m.end() : m.end() + 3]
            is_write = re.match(r"\s*=(?!=)", tail) is not None
            if is_write and (
                _INITIALIZER_NAME_RE.search(owner["name"])
                or _is_freshly_allocated(
                    owner["body"], m.group(1), m.start() - owner["start_byte"]
                )
            ):
                # A plain store into an object this thread just allocated and
                # has not published yet is unreachable by any other thread
                # (Objects/weakrefobject.c:65 init_weakref,
                # Objects/tupleobject.c tuple_iter).
                continue
        reported.add(field)
        findings.append(
            {
                "type": "atomic_plain_asymmetry",
                "ft_class": "T1",
                "file": rel,
                "member": field,
                "function": owner["name"] if owner else None,
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
    lock_held_functions = 0
    files_with_ft_regions = 0
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
        gil_only, ft_only = _gil_disabled_regions(source)
        if ft_only:
            files_with_ft_regions += 1
        rel = relpath(filepath, project_root)
        # T1 is file-scoped (needs cross-function field access map).
        findings.extend(_check_t1(source, rel, functions, gil_only))
        if not functions:
            continue

        files_analyzed += 1
        slot_names = _collect_iternext_names(source)

        lazy_sites: list[dict] = []
        for func in functions:
            total_functions += 1
            is_iter = _is_iternext(func["name"], slot_names)
            if is_iter:
                iternext_functions += 1
            if _caller_holds_lock(func["name"]):
                lock_held_functions += 1
            t3 = _check_t3(func, is_iter, tree, source_bytes, gil_only)
            if t3 is not None:
                t3["file"] = rel
                findings.append(t3)
            lazy_sites.extend(_collect_lazy_init_sites(func, source, gil_only))

        for f in _build_t2_findings(lazy_sites, tree, source_bytes):
            f["file"] = rel
            findings.append(f)

    findings = deduplicate_findings(findings)

    by_class: dict[str, int] = defaultdict(int)
    by_confidence: dict[str, int] = defaultdict(int)
    by_type: dict[str, int] = defaultdict(int)
    for f in findings:
        by_class[f["ft_class"]] += 1
        by_confidence[f["confidence"]] += 1
        by_type[f["type"]] += 1

    return build_report(
        project_root=project_root,
        scan_root=scan_root,
        files_analyzed=files_analyzed,
        functions_analyzed=total_functions,
        findings=findings,
        summary={
            "total_findings": len(findings),
            "by_class": dict(by_class),
            "by_type": dict(by_type),
            "by_confidence": dict(by_confidence),
        },
        iternext_functions=iternext_functions,
        # Denominators for the suppressions, so a low count is auditable:
        # `*_lock_held` callees are silent by convention, and files carrying
        # #ifdef Py_GIL_DISABLED regions had those arms modelled rather than
        # scanned blind.
        lock_held_functions=lock_held_functions,
        files_with_ft_regions=files_with_ft_regions,
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

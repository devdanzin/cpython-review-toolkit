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

* **T1 — field synchronisation asymmetry**: a struct field is written under
  synchronisation at one site and accessed plainly at another. Two spellings,
  one code path:

  ``guarded_writer_unguarded_reader`` — the synchronised write is under a
  critical section. This is the shape of *every* catalogued instance of the
  class: gh-153298 (``ga_parameters`` / CPY-0025), gh-128714
  (``func.__annotations__`` / CPY-0029), gh-153908 (``itertools count_repr``).

  ``atomic_plain_asymmetry`` — the synchronised write is an atomic macro
  (``_Py_atomic_*`` / ``FT_ATOMIC_*``); confirmed by itertools ``count_repr``
  (TSAN-0006). Also covers the *mixed-discipline* polarity: an atomic reader
  that takes no lock cannot be serialised against a plain writer under one
  (``_collectionsmodule.c`` ``dequeiter_len`` vs
  ``dequeiter_next_lock_held``, reproduced under TSan).

  One finding is emitted **per unsynchronised site**, and accesses are paired by
  the receiver's *declared struct type* — reporting one finding per bare field
  name per file cost two real findings in a single review run
  (``isliceobject.cnt`` displaced ``countobject.cnt``; thirteen
  ``dequeiterobject.counter`` sites collapsed to the one that was safe). A plain
  **pointer** read outranks a scalar one: a stale ``Py_ssize_t`` is a wrong
  number, a stale ``PyObject *`` handed to ``PyObject_Repr`` is a
  use-after-free.

Free-threading preprocessor regions are modelled throughout. A plain access in
the GIL-only arm of an ``#ifdef Py_GIL_DISABLED`` split is unreachable on the
free-threaded build and is never a race; conversely, a drop *elided* under
``#ifndef Py_GIL_DISABLED`` is a **fixed** T3 — that is the second canonical fix
strategy in ``Objects/`` (``tupleiter_next``, ``listiter_next``,
``reversed_next``) and its presence in a file is guarded-twin evidence.

Usage:
    python scan_ft_races.py [path] [--max-files N]
"""

import bisect
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

# CPython also locks through SCREAMING_CASE wrappers that expand to one of the
# above — `LOCK_WEAKREFS(obj)` (pycore_weakref.h), `ACQUIRE_LOCK(self)`,
# `LOCK_WEAKREFS_FOR_WR(self)`. The macro is usually defined in an internal
# header, so a file-local `#define` scan never sees it; matching the naming
# convention does.
_LOCK_MACRO_RE = re.compile(r"\b[A-Z][A-Z0-9_]*LOCK[A-Z0-9_]*\s*\(")

# Argument Clinic's `@critical_section` directive puts the lock in the
# *generated wrapper* (Modules/_io/clinic/bytesio.c.h), not in the `_impl`
# function that follows it — so the `_impl` looks completely unsynchronised
# while every one of its accesses in fact runs under a per-object lock. This is
# the single largest false-positive class the field-asymmetry rule has.
_CLINIC_CRITICAL_SECTION_RE = re.compile(r"^[ \t]*@critical_section\b", re.MULTILINE)

# Atomic access macros (CPython's own FT wrappers + the low-level primitives).
_ATOMIC_ACCESS_RE = re.compile(
    r"(?:_Py_atomic_\w+|FT_ATOMIC_\w+)\s*\(\s*&?\s*\w+\s*->\s*(\w+)"
)
# The mutating half of the atomic family: a store, an exchange, or an
# arithmetic/bitwise update.
_ATOMIC_WRITE_MACRO_RE = re.compile(
    r"(?:_Py_atomic_(?:store|exchange|compare_exchange|add|sub|and|or|xor)"
    r"|FT_ATOMIC_(?:STORE|EXCHANGE|COMPARE_EXCHANGE|ADD|SUB|AND|OR|XOR))",
    re.IGNORECASE,
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
    # `initialize` / `Initialize` is as much a constructor as `init`:
    # `_gil_initialize` stores `gil->interval` before the runtime is shared,
    # which is exactly what this suppression exists for.
    r"(?:^|_)(?:init\w*|new|alloc\w*|create|make)(?:_|$)|_New$|^new_|^init_",
    re.IGNORECASE,
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
    if any(tok in body for tok in _LOCK_TOKENS):
        return True
    return _LOCK_MACRO_RE.search(body) is not None


def _clinic_guarded_functions(raw_source: str, functions: list[dict]) -> set[str]:
    """Names of ``_impl`` functions whose clinic block asks for a lock.

    The directive sits in the ``/*[clinic input]*/`` comment immediately above
    the definition, so it must be read from the *raw* source.
    """
    guarded: set[str] = set()
    ordered = sorted(functions, key=lambda f: f["start_line"])
    for m in _CLINIC_CRITICAL_SECTION_RE.finditer(raw_source):
        line = raw_source.count("\n", 0, m.start()) + 1
        for func in ordered:
            if func["start_line"] > line:
                guarded.add(func["name"])
                break
    return guarded


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
        re.search(
            rf"(?:^|[;{{}}\n])\s*(?:struct\s+|union\s+)?"
            rf"{re.escape(type_name)}\s+\w+\s*;",
            source,
        )
        is not None
    )


_ALLOCATOR_CALL_RE = (
    # `it = (dequeiterobject *)type->tp_alloc(type, 0);` is the commonest
    # constructor spelling in Objects/ and Modules/. Without the `type->`
    # prefix and the `tp_alloc` name, every tp_alloc-derived local looked
    # shared and every constructor's field stores were reported as races.
    r"(?:[\w.]+\s*->\s*)?"
    r"(?:_?Py\w*(?:_New|_NEW|_Malloc|_MALLOC|_Alloc|_ALLOC)\w*"
    r"|_Py_FREELIST_POP|malloc|calloc"
    r"|tp_alloc|PyType_GenericAlloc)\s*\("
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
    r"CheckConsistency$|^dump_|_dump$|^_?Py\w*DebugDump|^print_"
    # `_PyObject_ManagedDictValidityCheck` is assert-only; `_Py_qsbr_after_fork`
    # and friends run in the single-threaded child right after fork().
    r"|ValidityCheck$|_after_fork$|_AfterFork$",
    re.IGNORECASE,
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


# Module-exec and interpreter-init functions run before the module object is
# reachable from any other thread.
_MODULE_INIT_NAME_RE = re.compile(r"_exec$|^PyInit_|^_?PyInit|_traverse$")

# The destructor family. `tp_dealloc` runs when the last reference is gone and
# `tp_clear` / `tp_traverse` run from the GC with the world stopped, so a plain
# field access there races nothing.
_DESTRUCTOR_NAME_RE = re.compile(
    r"_dealloc$|_traverse$|_clear$|_tp_free$|^\w+_free$"
    # Teardown spelled out rather than suffixed: `_PyObject_FinalizeUniqueIdPool`,
    # `py_hashentry_t_destroy_value`, `_PyXXX_Fini`.
    r"|finaliz\w*|destroy\w*|_fini$|Fini$|_teardown\b",
    re.IGNORECASE,
)

# A store of a constant sentinel: `x->f = NULL;` / `= 0;` / `= -1;`
_SENTINEL_STORE_RE = re.compile(r"^\s*=\s*(?:NULL|-?\d+)\s*;")

# `Py_BEGIN_CRITICAL_SECTION(op)` ... `Py_END_CRITICAL_SECTION()` spans, used
# for the one-hop caller check: `count_nextlong` takes no lock itself and is
# not named `*_lock_held`, but its only free-threaded caller wraps the call in
# a critical section, so its accesses *are* synchronised.
_CS_BEGIN_RE = re.compile(r"\bPy_BEGIN_CRITICAL_SECTION2?\s*\(")
_CS_END_RE = re.compile(r"\bPy_END_CRITICAL_SECTION2?\s*\(")

# A member declaration, used only to rank a pointer field above a scalar one:
# a stale pointer becomes a use-after-free, a stale Py_ssize_t becomes a wrong
# number.
_MEMBER_DECL_RE = re.compile(
    r"^[ \t]*(?:const\s+|volatile\s+|unsigned\s+|signed\s+|struct\s+|union\s+)*"
    r"[A-Za-z_]\w*\s*(\*+)?\s*([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*;[ \t]*$",
    re.MULTILINE,
)

_TYPE_NOISE = frozenset(
    {
        "return",
        "sizeof",
        "if",
        "while",
        "for",
        "switch",
        "const",
        "static",
    }
)

# Asymmetry caps — see the comment at the use site. Tuned against
# Objects/ + Modules/ + Python/ at CPython main 4f3be1b5777.
_MAX_EXPOSED_SITES = 4
_MAX_EXPOSED_FUNCTIONS = 2


def _critical_section_spans(source: str) -> list[tuple[int, int]]:
    """Character spans covered by a ``Py_BEGIN/END_CRITICAL_SECTION`` pair."""
    events = sorted(
        [(m.start(), "b") for m in _CS_BEGIN_RE.finditer(source)]
        + [(m.end(), "e") for m in _CS_END_RE.finditer(source)]
    )
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    for pos, kind in events:
        if kind == "b":
            stack.append(pos)
        elif stack:
            spans.append((stack.pop(), pos))
    return spans


def _pointer_fields(source: str) -> set[str]:
    """Member names declared as a pointer somewhere in this file."""
    return {m.group(2) for m in _MEMBER_DECL_RE.finditer(source) if m.group(1)}


def _receiver_type(func: dict, var: str) -> str | None:
    """Declared struct type of the receiver ``var`` inside ``func``.

    Pairing accesses by *member name alone* is what made ``islice``'s ``cnt``
    (``isliceobject``) look like ``count``'s ``cnt`` (``countobject``) and hid
    the real ``count_repr`` race behind an unrelated file-global name match.
    """
    for text in (func.get("parameters") or "", func.get("body") or ""):
        m = re.search(rf"\b([A-Za-z_]\w*)\s*\*+\s*{re.escape(var)}\b", text)
        if m and m.group(1) not in _TYPE_NOISE:
            return m.group(1)
    return None


def _line_starts(source: str) -> list[int]:
    """Offsets of every line start, for O(log n) offset→line lookup."""
    starts = [0]
    for m in re.finditer(r"\n", source):
        starts.append(m.end())
    return starts


def _caller_propagated_guards(
    source: str,
    functions: list[dict],
    cs_spans: list[tuple[int, int]],
    gil_only: list[tuple[int, int]],
    clinic_guarded: set[str],
) -> set[str]:
    """Functions whose every free-threaded call site runs under a lock.

    ``count_nextlong`` (Modules/itertoolsmodule.c) is the exemplar: it is not
    named ``*_lock_held`` and takes no lock itself, yet its only call reachable
    on the free-threaded build sits between ``Py_BEGIN_CRITICAL_SECTION(lz)``
    and ``Py_END_CRITICAL_SECTION()``.

    Propagation is transitive because the helper chains are: ``_io/textio.c``
    reaches ``_textiowrapper_writeflush`` from ``textiowrapper_read_chunk``,
    which is itself reached only from clinic ``@critical_section`` impls. A
    single hop leaves the whole chain looking unsynchronised.
    """
    by_name = {f["name"]: f for f in functions}
    starts = _line_starts(source)

    def _line_of(pos: int) -> int:
        return bisect.bisect_right(starts, pos)

    ordered = sorted(functions, key=lambda f: f["start_line"])
    calls: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", source):
        callee = m.group(1)
        if callee not in by_name:
            continue
        line = _line_of(m.start())
        if _in_ranges(line, gil_only):
            continue
        owner = _function_at_line(ordered, line)
        if owner is None or owner["name"] == callee:
            continue
        calls[callee].append((m.start(), owner["name"]))

    # Seeds: functions that demonstrably run with a lock held. `_has_lock` is
    # the weakest of the three and is a seed only — it never classifies the
    # seed's own accesses as guarded, because a function with one critical
    # section elsewhere says nothing about *this* line.
    guarded = {
        f["name"]
        for f in functions
        if _caller_holds_lock(f["name"])
        or f["name"] in clinic_guarded
        # A lock macro whose span this scanner cannot bracket
        # (`LOCK_WEAKREFS(obj)` ... `UNLOCK_WEAKREFS(obj)` around
        # `insert_after` / `insert_head` in Objects/weakrefobject.c).
        or _has_lock(f["body"])
    }
    propagated: set[str] = set()
    changed = True
    while changed:
        changed = False
        for func in functions:
            name = func["name"]
            if name in guarded or name in propagated:
                continue
            sites = calls.get(name)
            if not sites:
                continue
            if all(
                any(start <= pos < end for start, end in cs_spans)
                or caller in guarded
                or caller in propagated
                for pos, caller in sites
            ):
                propagated.add(name)
                changed = True
    return propagated


def _check_t1(
    source: str,
    rel: str,
    functions: list[dict],
    gil_only: list[tuple[int, int]],
) -> list[dict]:
    """T1: a struct field synchronised at one access site and not at another.

    The rule this replaces required the *guarded* access to be an atomic macro
    and reported at most one finding per field name per file. Both halves cost
    true positives, measured:

    * every catalogued instance of the bug — CPY-0025 (``ga_getitem``),
      CPY-0029 (``PyFunction_GetAnnotations``), gh-153908 (``count_repr``) — is
      **guarded writer vs unguarded reader**, and the guard is a critical
      section, not an atomic.
    * one-finding-per-field discarded ``count_repr:3678`` (TSAN-0006) in favour
      of an unrelated ``islice`` field that happened to share the name ``cnt``,
      and collapsed 13 ``_collectionsmodule.c`` ``counter`` sites into one — the
      one provably-safe site of the thirteen.

    So: classify every access of every field as ``atomic`` / ``locked`` /
    ``plain``, pair them by *receiver type* rather than by bare member name, and
    emit one finding **per unsynchronised site**, carrying the guarded twin.
    """
    clinic_guarded = _clinic_guarded_functions(source, functions)
    # Prose in a comment is not a field access (Objects/weakrefobject.c:112).
    source = strip_comments(source)
    atomic_matches = list(_ATOMIC_ACCESS_RE.finditer(source))
    cs_spans = _critical_section_spans(source)
    if not atomic_matches and not cs_spans:
        return []

    # Byte ranges covered by atomic-macro calls, so plain accesses inside an
    # atomic call are not counted as "plain". The span must cover the *whole*
    # call, not just up to the first field name: the second argument of
    # `_Py_atomic_store_ssize_release(&keys->dk_usable, keys->dk_usable - 1)`
    # is not an unsynchronised access (Objects/dictobject.c:249).
    atomic_spans = [_balanced_call_span(source, m.start()) for m in atomic_matches]
    # Debug-only readers: `assert()` compiles out with NDEBUG, and the dump /
    # consistency-check helpers run single-threaded from a debugger or a fatal
    # path (Objects/dictobject.c dump_entries, _PyDict_CheckConsistency).
    debug_spans = _call_spans(source, _DEBUG_ONLY_CALLS)
    exempt = sorted(atomic_spans + debug_spans)
    pointer_fields = _pointer_fields(source)

    def _line_of(pos: int) -> int:
        return source.count("\n", 0, pos) + 1

    def _covered(pos: int, spans: list[tuple[int, int]]) -> bool:
        return any(start <= pos < end for start, end in spans)

    propagated = _caller_propagated_guards(
        source, functions, cs_spans, gil_only, clinic_guarded
    )

    def _function_is_guarded(func: dict) -> bool:
        """Every access in this function runs with a lock already held.

        Deliberately *not* "the body contains a Py_BEGIN_CRITICAL_SECTION
        somewhere": that reading marks every field a big locking function
        touches as guarded, including the accesses outside its critical
        sections, and it is what turned this rule into a 1400-finding flood.
        An access inside a section is classified by span; a function that has a
        section but whose access sits outside it is *ambiguous* — neither a
        twin nor a finding.
        """
        name = func["name"]
        return _caller_holds_lock(name) or name in clinic_guarded or name in propagated

    # ---- collect every access, classified -------------------------------
    sites: list[dict] = []
    seen_positions: set[int] = set()

    def _record(
        pos: int,
        end: int,
        receiver: str,
        field: str,
        kind: str,
        *,
        is_write: bool | None = None,
    ) -> None:
        if pos in seen_positions:
            return
        seen_positions.add(pos)
        line = _line_of(pos)
        if _in_ranges(line, gil_only):
            # The GIL-only arm of an #ifdef Py_GIL_DISABLED split: the
            # free-threaded build never compiles it, so it cannot race
            # (Objects/tupleobject.c:1165, tupleiter_len).
            return
        owner = _function_at_line(functions, line)
        if owner is None or _DEBUG_ONLY_FN_RE.search(owner["name"]):
            return
        if _is_stack_local(owner["body"], receiver) or _points_at_stack_aggregate(
            owner, source, receiver
        ):
            # A by-value aggregate the caller owns on its stack is never
            # shared: `WFILE wf;` in Python/marshal.c, `struct worklist wl;`
            # and `gc_mark_args_t args;` in Python/gc_free_threading.c. The
            # lazy-init rule already knew this; the asymmetry rule did not, and
            # these were its single largest remaining false-positive class.
            return
        after = source[end : end + 24]
        sites.append(
            {
                "pos": pos,
                "line": line,
                "receiver": receiver,
                "recv_type": _receiver_type(owner, receiver),
                "field": field,
                "kind": kind,
                "function": owner["name"],
                "owner": owner,
                "is_write": (
                    re.match(r"\s*(?:=(?!=)|\+\+|--|[-+*/|&^]=|>>=|<<=)", after)
                    is not None
                    if is_write is None
                    else is_write
                ),
                "after": after,
            }
        )

    for m in atomic_matches:
        recv = re.search(r"(\w+)\s*->", m.group(0))
        _record(
            m.start(),
            m.end(),
            recv.group(1) if recv else "",
            m.group(1),
            "atomic",
            # `FT_ATOMIC_STORE_SSIZE_RELAXED(it->counter, n)` puts the value
            # after a comma, not after an `=`, so writes must be read off the
            # macro name.
            is_write=_ATOMIC_WRITE_MACRO_RE.search(m.group(0)) is not None,
        )
    for m in _PLAIN_MEMBER_RE.finditer(source):
        if _covered(m.start(), exempt):
            continue
        line = _line_of(m.start())
        owner = _function_at_line(functions, line)
        if owner is None:
            continue
        if _covered(m.start(), cs_spans) or _function_is_guarded(owner):
            kind = "locked"
        elif _has_lock(owner["body"]):
            # The function locks *something*, just not here. Too weak to be a
            # twin, too weak to be a finding.
            kind = "ambiguous"
        else:
            kind = "plain"
        _record(m.start(), m.end(), m.group(1), m.group(2), kind)

    # ---- group by (receiver type, field) --------------------------------
    groups: dict[tuple[str | None, str], list[dict]] = defaultdict(list)
    for site in sites:
        groups[(site["recv_type"], site["field"])].append(site)

    findings: list[dict] = []
    for (recv_type, field), group in groups.items():
        if recv_type is None:
            # Without a declared type the member name alone is not evidence
            # that two accesses touch the same struct (ACC-5, islice `cnt` vs
            # count `cnt`). Recorded in the denominators, never reported.
            continue
        atomic_sites = [s for s in group if s["kind"] == "atomic"]
        locked_sites = [s for s in group if s["kind"] == "locked"]
        plain_sites = [s for s in group if s["kind"] == "plain"]
        # "Guarded *writer*" is the load-bearing half of the shape. A field
        # that is only ever read under a lock and read plainly elsewhere is not
        # a race; something has to store to it. Requiring a synchronised write
        # is also what keeps the rule off the thousands of read-only accesses
        # to interpreter and type state.
        # A guarded write into an object the writing thread just allocated is
        # not evidence that the field needs protection — `deque_copy_impl`
        # stores `new_deque->maxlen` under a lock it holds for the *source*
        # deque, and `deque_iter` fills a fresh iterator. Neither makes a plain
        # read elsewhere a race.
        guarded_writes = [
            s
            for s in atomic_sites + locked_sites
            if s["is_write"] and not _is_pre_publication(s, source)
        ]
        if not guarded_writes:
            continue
        twin = guarded_writes[0]
        twin_is_atomic = twin["kind"] == "atomic"
        exposed = [s for s in plain_sites if not _is_pre_publication(s, source)]
        # The signal is *asymmetry* — one accessor guarded, its sibling not.
        # A field with a dozen unsynchronised accessors is an un-hardened
        # module, not a missed guard: reporting each site there is a flood that
        # buries the incomplete-fix cases the rule exists to find. Measured on
        # Objects/+Modules/+Python/, this cap is the difference between ~1100
        # findings and ~150, and it costs none of the reproduced ones.
        if (
            len(exposed) > _MAX_EXPOSED_SITES
            or len({s["function"] for s in exposed}) > _MAX_EXPOSED_FUNCTIONS
        ):
            continue
        for site in exposed:
            findings.append(
                _asymmetry_finding(
                    rel, recv_type, field, site, twin, twin_is_atomic, pointer_fields
                )
            )
        # The two synchronisation disciplines do not compose: a plain access
        # taken under a critical section races an atomic access that takes no
        # lock. `_collectionsmodule.c` `dequeiter_next_lock_held:1986` writes
        # `it->counter--` plainly inside Py_BEGIN_CRITICAL_SECTION2 while
        # `dequeiter_len:2049` reads it with FT_ATOMIC_LOAD_SSIZE outside any
        # section — reproduced under TSan.
        # Narrowed to the reproduced polarity: an atomic *reader* that takes no
        # lock cannot be serialised against a plain *writer* under a lock. The
        # mirror polarity (plain read under a lock, atomic write outside) is a
        # race too, but it did not survive hand-checking at volume and is left
        # to the plain-site families above.
        unlocked_atomics = [
            s
            for s in atomic_sites
            if not s["is_write"] and not _covered(s["pos"], cs_spans)
        ]
        if unlocked_atomics:
            for site in locked_sites:
                if not site["is_write"] or _is_pre_publication(site, source):
                    continue
                findings.append(
                    _asymmetry_finding(
                        rel,
                        recv_type,
                        field,
                        site,
                        unlocked_atomics[0],
                        True,
                        pointer_fields,
                        mixed_discipline=True,
                    )
                )
    return findings


def _is_pre_publication(site: dict, source: str) -> bool:
    """True if the access cannot be observed by another thread yet.

    Four shapes, all measured as false positives in the Modules/ run:
    a constructor or module-exec function, a receiver this thread just
    allocated, a destructor-family function, and a sentinel store with no
    prior read.
    """
    owner = site["owner"]
    name = owner["name"]
    if (
        _DESTRUCTOR_NAME_RE.search(name)
        or _MODULE_INIT_NAME_RE.search(name)
        # `PyType_Ready` runs to completion before the type object is
        # reachable from Python, so the slot-inheritance helpers it calls
        # populate fields nothing else can observe yet.
        or _TYPE_CONSTRUCTION_FN_RE.search(name)
    ):
        return True
    if not site["is_write"]:
        return False
    if _INITIALIZER_NAME_RE.search(name):
        # `dequeiter_new`'s own comment says it: "the iterator isn't visible
        # to any other threads yet".
        return True
    body_offset = max(site["pos"] - owner.get("start_byte", 0), 0)
    if _is_freshly_allocated(owner["body"], site["receiver"], body_offset):
        return True
    if _SENTINEL_STORE_RE.match(site["after"]):
        # `module_state->field_limit = 128 * 1024;` in `csv_exec`,
        # `self->hashcode = -1;` in a post-alloc constructor helper: a
        # constant store with no prior read of the field in this function is
        # initialisation, not a concurrent update.
        prior = re.search(
            rf"\b\w+\s*->\s*{re.escape(site['field'])}\b",
            owner["body"],
        )
        if prior is None or prior.start() >= body_offset:
            return True
    return False


def _asymmetry_finding(
    rel: str,
    recv_type: str,
    field: str,
    site: dict,
    twin: dict,
    twin_is_atomic: bool,
    pointer_fields: set[str],
    *,
    mixed_discipline: bool = False,
) -> dict:
    """Build one T1 finding for a single unsynchronised access site."""
    is_pointer = field in pointer_fields
    member = f"{recv_type}.{field}"
    # `member` is type-qualified so two same-named fields on different structs
    # cannot be confused; `access` is how the flagged line actually spells it,
    # which is what a reader (and the line-accuracy harness) looks for.
    access = f"{site['receiver']}->{field}"
    twin_ref = f"{twin['function']}:{twin['line']}"
    if mixed_discipline:
        return {
            "type": "atomic_plain_asymmetry",
            "ft_class": "T1",
            "file": rel,
            "member": member,
            "function": site["function"],
            "line": site["line"],
            "guarded_twin": twin_ref,
            "confidence": "medium" if is_pointer else "low",
            "detail": (
                f"'{member}' (`{access}`) is {'written' if site['is_write'] else 'read'} "
                "plainly here under a critical section, but is accessed with an "
                f"atomic macro and no lock at {twin_ref}. The two "
                "synchronisation disciplines do not compose: an atomic access "
                "racing a non-atomic one is a data race by definition, and the "
                "atomic spelling is proof the maintainers already knew the "
                "field was shared (reproduced under TSan at "
                "_collectionsmodule.c dequeiter_next_lock_held / dequeiter_len). "
                "Use FT_ATOMIC_* here too."
            ),
        }
    if twin_is_atomic:
        return {
            "type": "atomic_plain_asymmetry",
            "ft_class": "T1",
            "file": rel,
            "member": member,
            "function": site["function"],
            "line": site["line"],
            "guarded_twin": twin_ref,
            "confidence": "medium" if is_pointer else "low",
            "detail": (
                f"'{member}' (`{access}`) is accessed via an atomic macro (_Py_atomic_* / "
                f"FT_ATOMIC_*) at {twin_ref} but is "
                f"{'written' if site['is_write'] else 'read'} plainly here, "
                "outside any critical section — this access is unsynchronized "
                "(cf. itertools count_repr / TSAN-0006). "
                + (
                    "The field is a pointer, so the loser of the race follows a "
                    "stale pointer: a use-after-free, not a stale value. "
                    if is_pointer
                    else ""
                )
                + "Make every access to a concurrently-shared field atomic, or "
                "hold the same lock at both sites."
            ),
        }
    return {
        "type": "guarded_writer_unguarded_reader",
        "ft_class": "T1",
        "file": rel,
        "member": member,
        "function": site["function"],
        "line": site["line"],
        "guarded_twin": twin_ref,
        "confidence": "medium" if is_pointer else "low",
        "detail": (
            f"'{member}' (`{access}`) is accessed under a critical section at {twin_ref} "
            f"but is {'written' if site['is_write'] else 'read'} plainly here, "
            "with nothing synchronising it. A lock held by only some accessors "
            "of a field serialises nothing: the guarded and unguarded accessors "
            "race each other. This is the shape of gh-153298 (ga_parameters / "
            "CPY-0025), gh-128714 (func.__annotations__ / CPY-0029) and "
            "gh-153908 (itertools count_repr). "
            + (
                "The field is a pointer and the guarded twin can free it, so "
                "the unguarded reader can hand a freed object to arbitrary "
                "Python — a use-after-free, which an atomic load cannot fix; "
                "take the same critical section. "
                if is_pointer
                else "Py_ssize_t does not tear on LP64, so the visible effect "
                "is a stale value rather than memory unsafety — one "
                "FT_ATOMIC_* on each access is the fix. "
            )
            + f"Guarded twin: {twin_ref}."
        ),
    }


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

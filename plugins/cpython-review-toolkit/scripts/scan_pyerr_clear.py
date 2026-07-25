#!/usr/bin/env python3
"""Scan CPython C source for ``PyErr_Clear()`` calls that swallow a live exception.

Three rules, in decreasing precision:

``pyerr_clear_in_dealloc`` (OOM class O3)
    A clear inside a ``tp_dealloc`` / ``tp_clear`` / ``tp_finalize`` /
    ``tp_traverse`` function that is not *positionally* bracketed by a
    save/restore pair. Destructors and finalizers run while an exception may
    already be in flight (an object's last reference is commonly dropped
    mid-exception-handling), so an unguarded clear there silently swallows the
    caller's live ``MemoryError`` / ``KeyboardInterrupt`` / ``SystemExit``.
    Confirmed exemplars: ``deque_clear`` (Modules/_collectionsmodule.c,
    OOM-0039) and ``context_tp_dealloc`` (Modules/_contextvarsmodule.c,
    gh-152083).

``pyerr_clear_on_success_path`` (the gh-146102 class)
    A clear that no enclosing conditional guards with an error test. Upstream
    established on 2026-06-15 (``b3b7c0025be``, ``mutablemapping_add_pairs``)
    that a clear on a path where nothing can be set is itself a bug: the only
    exception it can discard is the *caller's*. The test here is a dominance
    test over the enclosing ``if`` / ``else`` / loop conditions, not a text
    heuristic. Prescribed fix: delete the clear, add ``assert(!PyErr_Occurred())``.

``pyerr_clear_unfiltered_after_python_call``
    A clear on the failure path of a call that runs **arbitrary Python** (a
    user ``__hash__`` / ``__buffer__`` / ``__index__``, an import hook, a slot
    dispatched through ``bf_getbuffer`` and friends), with no
    ``PyErr_ExceptionMatches`` narrowing between the failure test and the clear.
    The ``-1`` / ``NULL`` return only means *something* was raised, so the clear
    discards ``MemoryError`` / ``KeyboardInterrupt`` / ``RecursionError`` along
    with the expected ``TypeError``. The narrowing gate is what makes this
    affordable: a clear after a *sentinel-returning lookup*
    (``PyObject_GetAttr``, ``PyDict_GetItemWithError``, …) is idiomatic and is
    what the narrowing filters out.

Note on overlap: ``scan_error_paths.py`` carries an ``unconditional_pyerr_clear``
rule that can also fire on a non-destructor unfiltered clear. The two rules are
independent by design (different gates); dedupe cross-scanner reports by
``(file, line)``.

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

# ---------------------------------------------------------------------------
# Token tables
# ---------------------------------------------------------------------------

# CPython internals overwhelmingly use the tstate-passing private alias, and
# the two spellings are disjoint in practice: Objects/ has 83 bare
# ``PyErr_Clear`` and Python/ has 70 ``_PyErr_Clear(tstate)``. Matching only
# the public name made every one of the latter invisible.
_CLEAR_NAMES = frozenset({"PyErr_Clear", "_PyErr_Clear"})

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

# Capturing the pending exception. A clear is bracketed only if a save appears
# *before* it and a restore *after* it — whole-function matching would let a
# large destructor that saves/restores correctly in one region hide a second,
# unguarded clear the bracket never covers.
_SAVE_APIS = frozenset(
    {
        "PyErr_Fetch",
        "_PyErr_Fetch",
        "PyErr_GetRaisedException",
        "_PyErr_GetRaisedException",
        "PyErr_GetHandledException",
        "_PyErr_GetHandledException",
    }
)
_RESTORE_APIS = frozenset(
    {
        "PyErr_Restore",
        "_PyErr_Restore",
        "PyErr_SetRaisedException",
        "_PyErr_SetRaisedException",
        "PyErr_SetHandledException",
        "_PyErr_SetHandledException",
    }
)
# APIs that consume the pending exception in place (report it, or chain it into
# a new one). These don't bracket anything; a clear in the same block as one is
# deliberate belt-and-braces, so it is suppressed positionally by block.
_REPORT_APIS = frozenset(
    {
        "PyErr_WriteUnraisable",
        "_PyErr_WriteUnraisableMsg",
        "PyErr_FormatUnraisable",
        "PyErr_ChainExceptions",
        "_PyErr_ChainExceptions",
        "_PyErr_ChainExceptions1",
        "_PyErr_ChainExceptions1Tstate",
    }
)

# Naming the exception state in a condition is an error test by construction.
_ERROR_STATE_APIS = frozenset(
    {
        "PyErr_Occurred",
        "_PyErr_Occurred",
        "PyErr_ExceptionMatches",
        "_PyErr_ExceptionMatches",
        "PyErr_GivenExceptionMatches",
        "_PyErr_GivenExceptionMatches",
    }
)

# The narrowing gate: an explicit "is this the exception I expected?" test.
_NARROWING_APIS = frozenset(
    {
        "PyErr_ExceptionMatches",
        "_PyErr_ExceptionMatches",
        "PyErr_GivenExceptionMatches",
        "_PyErr_GivenExceptionMatches",
    }
)

# Non-API error sentinels that still constitute an error test.
_ERROR_SIGNAL_TOKENS = ("DKIX_ERROR",)

# Leaving *some* exception pending after the clear. For rule 2 — a clear that
# no error test dominates — this is enough to suppress: the function is a
# deliberate replacer (`_PyErr_SetKeyError`, `_PyErr_FormatV`,
# `_set_BlockingIOError` all clear first precisely because the API that builds
# the replacement must not run with an exception set).
_RERAISE_PREFIXES = (
    "PyErr_Set",
    "_PyErr_Set",
    "PyErr_Format",
    "_PyErr_Format",
    "PyErr_NoMemory",
    "_PyErr_NoMemory",
    "PyErr_BadInternalCall",
    "_PyErr_BadInternalCall",
    "PyErr_BadArgument",
)

# For rule 3 the same test is *inverted*. There the clear sits in the failure
# branch of a call that ran arbitrary Python, so a bare
# `PyErr_SetString(PyExc_ValueError, "...")` substitutes a fixed, less specific
# exception for whatever the user's `__index__` / `__hash__` raised — and drops
# the context chain with it. That substitution IS the bug; treating it as a
# mitigation suppressed 3 of the 4 true positives in
# Modules/itertoolsmodule.c islice_new. Only a re-raise that *carries the
# discarded information forward* — restore, chain, or derive from an
# out-of-band channel (errno / GetLastError) the Python exception was masking
# — suppresses there.
_PRESERVING_RERAISE_PREFIXES = (
    "PyErr_SetFromErrno",
    "_PyErr_SetFromErrno",
    "PyErr_SetExcFromWindowsErr",
    "PyErr_SetFromWindowsErr",
    "PyErr_SetImportError",
    "PyErr_SetRaisedException",
    "_PyErr_SetRaisedException",
    "PyErr_SetExcInfo",
    "PyErr_Restore",
    "_PyErr_Restore",
    "PyErr_ChainExceptions",
    "_PyErr_ChainExceptions",
)

# Calls that can dispatch into arbitrary Python. A failure from one of these
# carries *whatever the user's code raised*, so clearing it without narrowing
# discards MemoryError / KeyboardInterrupt / RecursionError too.
_PYTHON_REACHING_CALLS = frozenset(
    {
        "PyObject_Hash",
        "_PyObject_HashFast",
        "PyObject_GetBuffer",
        "PyObject_RichCompare",
        "PyObject_RichCompareBool",
        "PyObject_GetAttr",
        "PyObject_GetAttrString",
        "_PyObject_GetAttrId",
        "PyObject_SetAttr",
        "PyObject_SetAttrString",
        "PyObject_GetItem",
        "PyObject_SetItem",
        "PyObject_DelItem",
        "PyNumber_Index",
        "_PyNumber_Index",
        "PyNumber_Long",
        "PyNumber_AsSsize_t",
        "_PyObject_LookupSpecial",
        "PyObject_Str",
        "PyObject_Repr",
        "PyObject_Bytes",
        "PyObject_IsTrue",
        "PyObject_Length",
        "PyObject_Size",
        "PyIter_Next",
        "PyType_Ready",
        "_PyImport_TryLoadLazySubmodule",
        "PyImport_Import",
        "PyImport_ImportModule",
    }
)
_PYTHON_REACHING_PREFIXES = (
    "PyObject_Call",
    "_PyObject_Call",
    "PyObject_Vectorcall",
    "_PyObject_Vectorcall",
)

# A call made through one of these struct fields is a type-slot dispatch, i.e.
# arbitrary Python by definition (``(*pb->bf_getbuffer)(obj, &view, ...)``).
_SLOT_FIELD_PREFIXES = ("bf_", "tp_", "nb_", "sq_", "mp_", "am_")

# ...unless the receiver is a statically-known concrete type object, in which
# case the slot is a fixed C function and no user code runs
# (``PyUnicode_Type.tp_hash(key)``).
_STATIC_TYPE_RE = re.compile(r"^&?_?Py[A-Za-z0-9_]*_Type$")

# Statements that end a branch, so the code after the enclosing ``if`` is
# dominated by the negation of its condition.
_TERMINATORS = frozenset(
    {"return_statement", "goto_statement", "break_statement", "continue_statement"}
)

_COMPARISONS = frozenset({"==", "!=", "<", "<=", ">", ">=", "&", "|"})
_CONNECTIVES = frozenset({"&&", "||"})

# A call compared against one of these is being checked for failure, whoever
# wrote it — including the static file-local helpers that make up most of a
# CPython module (``if (random_seed_urandom(self) < 0)``).
_SENTINELS = frozenset({"NULL", "0", "-1"})

# CPython spells most early returns as a macro, which tree-sitter parses as an
# ordinary expression statement rather than a return_statement. Without these a
# guard clause like ``if (...) { Py_RETURN_TRUE; }`` doesn't look terminating.
_MACRO_TERMINATOR_RE = re.compile(
    r"^\s*(Py_RETURN_\w+|Py_UNREACHABLE\s*\(|Py_FatalError\s*\(|_Py_FatalError\w*\s*\()"
)

_MACRO_TAIL_RE = re.compile(r"^_?Py[A-Za-z0-9]*_[A-Z0-9_]+$")


# ---------------------------------------------------------------------------
# Small AST helpers
# ---------------------------------------------------------------------------


def _unwrap_callee(node, source_bytes: bytes) -> tuple[str, str]:
    """Classify a call's callee.

    Returns ``(kind, name)`` where kind is:
      - ``"name"``        a plain identifier (``PyObject_Hash``)
      - ``"field"``       a slot dispatched off a runtime object
                          (``(*pb->bf_getbuffer)(...)``) — arbitrary Python
      - ``"static_slot"`` a slot read off a statically-known concrete type
                          (``PyUnicode_Type.tp_hash``) — fallible in principle
                          but never dispatches into user code
      - ``"other"``
    """
    cur = node
    for _ in range(6):
        if cur is None:
            break
        if cur.type == "parenthesized_expression":
            kids = [c for c in cur.children if c.type not in ("(", ")")]
            cur = kids[0] if kids else None
            continue
        if cur.type in ("pointer_expression", "unary_expression"):
            cur = cur.child_by_field_name("argument")
            continue
        break
    if cur is None:
        return ("other", "")
    if cur.type == "identifier":
        return ("name", get_node_text(cur, source_bytes))
    if cur.type == "field_expression":
        field = cur.child_by_field_name("field")
        if field is not None:
            receiver = cur.child_by_field_name("argument")
            recv_text = (
                get_node_text(receiver, source_bytes).strip() if receiver else ""
            )
            kind = "static_slot" if _STATIC_TYPE_RE.match(recv_text) else "field"
            return (kind, get_node_text(field, source_bytes))
    return ("other", get_node_text(cur, source_bytes))


def _call_sites(node, source_bytes: bytes) -> list[dict]:
    """Every call in ``node``: kind, name, line, byte offsets."""
    sites: list[dict] = []
    for call in walk_descendants(node, type_filter="call_expression"):
        fn_node = call.child_by_field_name("function")
        if fn_node is None:
            continue
        kind, name = _unwrap_callee(fn_node, source_bytes)
        sites.append(
            {
                "kind": kind,
                "name": name,
                "line": call.start_point[0] + 1,
                "start_byte": call.start_byte,
                "end_byte": call.end_byte,
                "node": call,
            }
        )
    return sites


def _is_python_reaching(kind: str, name: str) -> bool:
    """True if a call to ``name`` can dispatch into user-written Python."""
    if kind == "field":
        return name.startswith(_SLOT_FIELD_PREFIXES)
    if kind != "name":
        return False
    if name in _PYTHON_REACHING_CALLS:
        return True
    return name.startswith(_PYTHON_REACHING_PREFIXES)


def _is_fallible_api(kind: str, name: str) -> bool:
    """True if a call looks like a CPython API that can set an exception.

    Deliberately excludes ``Py*_Check`` predicates and all-caps accessor macros
    (``Py_SIZE``, ``PyList_GET_ITEM``), which cannot fail — treating those as
    error tests would suppress genuine success-path clears.
    """
    if kind in ("field", "static_slot"):
        return name.startswith(_SLOT_FIELD_PREFIXES)
    if kind != "name":
        return False
    if not name.startswith(("Py", "_Py")):
        return False
    if name.endswith(("_Check", "_CheckExact")):
        return False
    return not _MACRO_TAIL_RE.match(name)


def _lvalue_key(text: str) -> str:
    """Normalize an assignment target so a condition can be matched against it."""
    return re.sub(r"\s+", "", text)


def _collect_call_assignments(body_node, source_bytes: bytes) -> dict[str, list[tuple]]:
    """Map lvalue -> [(line, kind, callee_name)] for call-valued assignments.

    Covers ``T x = call(...);`` and ``x = call(...);``, and keys non-identifier
    lvalues by their source text so the two idioms CPython uses constantly —
    ``interp->dict = PyDict_New()`` tested as ``if (interp->dict == NULL)`` and
    ``*pmod = parse(...)`` tested as ``if (*pmod == NULL)`` — resolve too.
    Without them those error paths read as success paths.
    """
    assignments: dict[str, list[tuple]] = defaultdict(list)

    def _record(target_node, value_node, declarator: bool) -> None:
        if target_node is None or value_node is None:
            return
        if value_node.type != "call_expression":
            return
        cur = target_node
        if declarator:
            # Peel pointer/array declarators to reach the identifier.
            for _ in range(4):
                if cur.type == "identifier":
                    break
                nxt = cur.child_by_field_name("declarator")
                if nxt is None:
                    return
                cur = nxt
            if cur.type != "identifier":
                return
        fn_node = value_node.child_by_field_name("function")
        if fn_node is None:
            return
        kind, name = _unwrap_callee(fn_node, source_bytes)
        key = _lvalue_key(get_node_text(cur, source_bytes))
        assignments[key].append((value_node.start_point[0] + 1, kind, name))

    for node in walk_descendants(body_node, type_filter="init_declarator"):
        _record(
            node.child_by_field_name("declarator"),
            node.child_by_field_name("value"),
            True,
        )
    for node in walk_descendants(body_node, type_filter="assignment_expression"):
        _record(
            node.child_by_field_name("left"), node.child_by_field_name("right"), False
        )
    return assignments


def _latest_assignment(assignments: dict, lvalue: str, before_line: int):
    """The most recent call-valued assignment to ``lvalue`` at or above a line."""
    best = None
    for line, kind, name in assignments.get(_lvalue_key(lvalue), ()):
        if line <= before_line and (best is None or line > best[0]):
            best = (line, kind, name)
    return best


# ---------------------------------------------------------------------------
# Dominance: the conditions that guard a clear
# ---------------------------------------------------------------------------


def _enclosing_conditions(call_node, body_node, source_bytes: bytes) -> list[dict]:
    """Conditions of every ``if`` / loop / ``switch`` enclosing a call.

    Innermost first. For a clear in an ``else`` branch the governing condition
    is still recorded (with ``branch="else"``) — an ``else`` of an error test is
    the *success* side, which is why the branch is carried through.
    """
    conditions: list[dict] = []
    child = call_node
    node = call_node.parent
    while node is not None and node.id != body_node.id:
        if node.type in (
            "if_statement",
            "while_statement",
            "do_statement",
            "for_statement",
            "switch_statement",
        ):
            cond = node.child_by_field_name("condition")
            # A clear sitting inside the condition itself isn't guarded by it.
            if cond is not None and not (
                cond.start_byte <= call_node.start_byte < cond.end_byte
            ):
                branch = "then"
                if node.type == "if_statement":
                    alt = node.child_by_field_name("alternative")
                    if alt is not None and alt.start_byte <= child.start_byte:
                        branch = "else"
                conditions.append(
                    {
                        "node": cond,
                        "text": get_node_text(cond, source_bytes),
                        "branch": branch,
                        "line": cond.start_point[0] + 1,
                        "start_byte": node.start_byte,
                    }
                )
        child = node
        node = node.parent
    return conditions


def _statement_terminates(stmt, source_bytes: bytes) -> bool:
    """True if a single statement ends control flow in its branch."""
    if stmt.type in _TERMINATORS:
        return True
    if stmt.type == "expression_statement":
        return bool(_MACRO_TERMINATOR_RE.match(get_node_text(stmt, source_bytes)))
    return False


def _branch_terminates(branch_node, source_bytes: bytes) -> bool:
    """True if a branch ends in return / goto / break / continue / Py_RETURN_*."""
    if branch_node is None:
        return False
    if _statement_terminates(branch_node, source_bytes):
        return True
    if branch_node.type != "compound_statement":
        return False
    for stmt in reversed(branch_node.children):
        if stmt.type in ("}", "{", "comment"):
            continue
        return _statement_terminates(stmt, source_bytes)
    return False


def _preceding_guard_conditions(
    call_node, body_node, source_bytes: bytes
) -> list[dict]:
    """Conditions of *terminating* guard clauses that precede a call in scope.

    ``if (!PyErr_ExceptionMatches(PyExc_KeyError)) { return -1; }`` followed by
    ``PyErr_Clear()`` dominates the clear just as firmly as wrapping it in an
    ``if`` would: reaching the clear implies the negation of the condition.
    Without this, the ubiquitous CPython guard-clause style makes every
    narrowed clear look like a success-path clear. Polarity does not matter —
    either way the code *tested* the error state before clearing.
    """
    conditions: list[dict] = []
    child = call_node
    node = call_node.parent
    while node is not None:
        if node.type == "compound_statement" or node.id == body_node.id:
            for stmt in node.children:
                if stmt.end_byte > child.start_byte:
                    break
                if stmt.type != "if_statement":
                    continue
                cond = stmt.child_by_field_name("condition")
                if cond is None:
                    continue
                # A test that names the exception state counts even when the
                # branch exits through a macro the parser can't see through
                # (FAIL(...), Py_RETURN_*): the author *did* reason about
                # whether an exception is live, so "success path" is not a
                # claim this scanner can make.
                cond_text = get_node_text(cond, source_bytes)
                if not any(
                    api in cond_text for api in _ERROR_STATE_APIS
                ) and not _branch_terminates(
                    stmt.child_by_field_name("consequence"), source_bytes
                ):
                    continue
                conditions.append(
                    {
                        "node": cond,
                        "text": cond_text,
                        "branch": "fallthrough",
                        "line": cond.start_point[0] + 1,
                        "start_byte": stmt.start_byte,
                    }
                )
        if node.id == body_node.id:
            break
        child = node
        node = node.parent
    return conditions


def _asserts_error_pending(call_node, body_node, source_bytes: bytes) -> bool:
    """True if an ``assert(PyErr_Occurred())`` precedes the call in scope.

    The negated form ``assert(!PyErr_Occurred())`` is the gh-146102 *fix*, so
    only the positive assertion counts as "an error is known to be live here".
    """
    for site in _call_sites(body_node, source_bytes):
        if site["kind"] != "name" or site["name"] != "assert":
            continue
        if site["start_byte"] >= call_node.start_byte:
            continue
        text = get_node_text(site["node"], source_bytes)
        if "!" in text:
            continue
        if any(api in text for api in _ERROR_STATE_APIS):
            return True
    return False


def _condition_signals(cond_node, source_bytes: bytes, assignments: dict) -> dict:
    """Classify a condition as an error test and name the failing call.

    Returns ``{"errstate": bool, "calls": [(kind, name), ...]}`` where ``calls``
    holds the fallible calls whose result the condition tests — either called
    inline (``if (PyObject_GetBuffer(...) < 0)``) or via a sentinel comparison
    against a call-assigned identifier (``h = PyObject_Hash(op); if (h == -1)``).
    """
    text = get_node_text(cond_node, source_bytes)
    errstate = any(tok in text for tok in _ERROR_SIGNAL_TOKENS)
    calls: list[dict] = []
    sentinel_compared = _sentinel_compared_calls(cond_node, source_bytes)
    polarity = _polarities(cond_node, source_bytes)

    for site in _call_sites(cond_node, source_bytes):
        if site["kind"] == "name" and site["name"] in _ERROR_STATE_APIS:
            errstate = True
            continue
        # A call is an error test either because it is a fallible CPython API,
        # or because whatever it is, its result is compared against an error
        # sentinel right here.
        if _is_fallible_api(site["kind"], site["name"]) or (
            site["start_byte"] in sentinel_compared
        ):
            key = _lvalue_key(get_node_text(site["node"], source_bytes))
            calls.append(
                {
                    "kind": site["kind"],
                    "name": site["name"],
                    "then_failure": polarity.get(key, True),
                }
            )

    cond_line = cond_node.start_point[0] + 1

    for text in _tested_operands(cond_node, source_bytes):
        found = _latest_assignment(assignments, text, cond_line)
        if found is not None:
            calls.append(
                {
                    "kind": found[1],
                    "name": found[2],
                    "then_failure": polarity.get(_lvalue_key(text), True),
                }
            )

    return {"errstate": errstate, "calls": calls}


def _comparison_is_failure(op: str, literal: str) -> bool | None:
    """Does ``operand op literal`` being true mean the operand's call failed?"""
    if literal == "NULL":
        return {"==": True, "!=": False}.get(op)
    if literal == "-1":
        return {"==": True, "<=": True, "!=": False, ">": False, ">=": False}.get(op)
    if literal == "0":
        return {
            "<": True,
            "<=": True,
            "!=": True,
            "==": False,
            ">": False,
            ">=": False,
        }.get(op)
    return None


def _polarities(cond_node, source_bytes: bytes) -> dict[str, bool]:
    """Map tested-expression text -> "the then-branch is the failure branch".

    ``if (obj == NULL)`` puts the failure in the then-branch; ``if (module)``
    puts it in the else-branch. Rule 3 needs this: a clear inside
    ``if (module) { ... }`` is not reacting to the import failing.
    """
    out: dict[str, bool] = {}

    def _walk(node, negated: bool) -> None:
        if node is None:
            return
        if node.type == "parenthesized_expression":
            for child in node.children:
                if child.type not in ("(", ")"):
                    _walk(child, negated)
            return
        if node.type == "unary_expression":
            op_node = node.child_by_field_name("operator")
            if op_node is not None and get_node_text(op_node, source_bytes) == "!":
                _walk(node.child_by_field_name("argument"), not negated)
                return
            out.setdefault(_lvalue_key(get_node_text(node, source_bytes)), negated)
            return
        if node.type == "binary_expression":
            op_node = node.child_by_field_name("operator")
            op = get_node_text(op_node, source_bytes) if op_node else ""
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if op in _CONNECTIVES:
                _walk(left, negated)
                _walk(right, negated)
                return
            if op in _COMPARISONS and left is not None and right is not None:
                left_text = get_node_text(left, source_bytes).strip()
                right_text = get_node_text(right, source_bytes).strip()
                for operand, literal in (
                    (left_text, right_text),
                    (right_text, left_text),
                ):
                    verdict = _comparison_is_failure(op, literal)
                    if verdict is not None:
                        out.setdefault(_lvalue_key(operand), verdict != negated)
            return
        out.setdefault(_lvalue_key(get_node_text(node, source_bytes)), negated)

    _walk(cond_node, False)
    return out


def _sentinel_compared_calls(cond_node, source_bytes: bytes) -> set[int]:
    """Start-bytes of calls whose result is compared against an error sentinel."""
    found: set[int] = set()
    for binexp in walk_descendants(cond_node, type_filter="binary_expression"):
        op_node = binexp.child_by_field_name("operator")
        op = get_node_text(op_node, source_bytes) if op_node else ""
        if op not in _COMPARISONS:
            continue
        left = binexp.child_by_field_name("left")
        right = binexp.child_by_field_name("right")
        if left is None or right is None:
            continue
        for call_side, lit_side in ((left, right), (right, left)):
            if call_side.type != "call_expression":
                continue
            if get_node_text(lit_side, source_bytes).strip() in _SENTINELS:
                found.add(call_side.start_byte)
    return found


def _tested_operands(node, source_bytes: bytes) -> list[str]:
    """The expressions a condition *directly* tests.

    Descends through parentheses, ``!``, ``&&`` and ``||`` and returns the leaf
    operands of the comparisons and truth tests. Deliberately does **not**
    descend into a call's arguments: ``if (!equiv_shape(vv, ww))`` tests the
    predicate's result, not ``ww`` — treating an argument as "tested" would
    suppress the one true success-path clear in Objects/.
    """
    if node is None:
        return []
    if node.type == "parenthesized_expression":
        out: list[str] = []
        for child in node.children:
            if child.type not in ("(", ")"):
                out.extend(_tested_operands(child, source_bytes))
        return out
    if node.type == "unary_expression":
        op_node = node.child_by_field_name("operator")
        if op_node is not None and get_node_text(op_node, source_bytes) == "!":
            return _tested_operands(node.child_by_field_name("argument"), source_bytes)
        return [get_node_text(node, source_bytes).strip()]
    if node.type == "binary_expression":
        op_node = node.child_by_field_name("operator")
        op = get_node_text(op_node, source_bytes) if op_node else ""
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if op in _CONNECTIVES:
            return _tested_operands(left, source_bytes) + _tested_operands(
                right, source_bytes
            )
        if op in _COMPARISONS:
            return [
                get_node_text(n, source_bytes).strip()
                for n in (left, right)
                if n is not None
            ]
        return []
    return [get_node_text(node, source_bytes).strip()]


# ---------------------------------------------------------------------------
# Rule 1 — destructor family
# ---------------------------------------------------------------------------


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
    """Find every ``PyErr_Clear()`` / ``_PyErr_Clear(tstate)`` call in a body."""
    calls: list[dict] = []
    for node in walk_descendants(func["body_node"], type_filter="call_expression"):
        fn_node = node.child_by_field_name("function")
        if fn_node is None:
            continue
        kind, name = _unwrap_callee(fn_node, source_bytes)
        if kind == "name" and name in _CLEAR_NAMES:
            calls.append({"node": node, "line": node.start_point[0] + 1, "name": name})
    return calls


def _enclosing_block(node, body_node):
    """The innermost ``compound_statement`` containing ``node``."""
    cur = node.parent
    while cur is not None:
        if cur.type == "compound_statement":
            return cur
        if cur.id == body_node.id:
            return cur
        cur = cur.parent
    return body_node


def _guard_brackets_clear(func: dict, call: dict, source_bytes: bytes) -> bool:
    """True if a save/restore pair *positionally* brackets this clear.

    Whole-function matching (``any(api in body)``) is unsound: a big destructor
    that saves and restores around one region will silently suppress a second
    clear the bracket never covers. Requires a save strictly above the clear and
    a restore strictly below it. A report/chain API in the clear's own block
    also counts — those consume the exception in place.
    """
    body_node = func["body_node"]
    line = call["line"]
    saved_above = False
    restored_below = False
    block = _enclosing_block(call["node"], body_node)

    for site in _call_sites(body_node, source_bytes):
        if site["kind"] != "name":
            continue
        name = site["name"]
        if name in _SAVE_APIS and site["line"] < line:
            saved_above = True
        elif name in _RESTORE_APIS and site["line"] > line:
            restored_below = True
        elif name in _REPORT_APIS and (
            block.start_byte <= site["start_byte"] < block.end_byte
        ):
            return True
    return saved_above and restored_below


def _check_destructor(func: dict, kind: str, source_bytes: bytes, tree) -> list[dict]:
    """Flag unguarded ``PyErr_Clear()`` in a destructor-family function."""
    findings: list[dict] = []
    # dealloc/finalize/clear run during teardown that may be mid-exception;
    # traverse is meant to be side-effect-free, so a clear there is odd but
    # lower-severity.
    confidence = "medium" if kind == "tp_traverse" else "high"
    for call in _find_pyerr_clear_calls(func, source_bytes):
        if _guard_brackets_clear(func, call, source_bytes):
            continue
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
                    f"{call['name']}() in {kind} function '{func['name']}' with no "
                    "save/restore of the exception state bracketing this call — "
                    "silently swallows an in-flight MemoryError / "
                    "KeyboardInterrupt / SystemExit. Bracket the risky work with "
                    "PyErr_GetRaisedException()/PyErr_SetRaisedException() (or "
                    "report via PyErr_WriteUnraisable)."
                ),
            }
        )
    return findings


# ---------------------------------------------------------------------------
# Rules 2 and 3 — outside the destructor family
# ---------------------------------------------------------------------------


def _reports_exception_nearby(call: dict, body_node, source_bytes: bytes) -> bool:
    """True if the clear's own if/else consumes the exception in the other arm.

    ``print_or_clear_traceback()`` in Modules/_sqlite is the canonical shape:
    ``if (tracebacks_enabled) PyErr_FormatUnraisable(...); else PyErr_Clear();``
    — the whole function exists to consume a pending exception one way or the
    other, so the clear is deliberate.
    """
    scope = call["node"].parent
    while scope is not None and scope.id != body_node.id:
        if scope.type == "if_statement":
            break
        scope = scope.parent
    if scope is None or scope.type != "if_statement":
        return False
    for site in _call_sites(scope, source_bytes):
        if site["kind"] == "name" and site["name"] in _REPORT_APIS:
            return True
    return False


def _reraises_after(
    call: dict, body_node, source_bytes: bytes, *, preserving_only: bool = False
) -> bool:
    """True if the clear is followed by a re-raise in its block.

    ``preserving_only`` selects the rule-3 semantics: only a re-raise that
    carries the discarded exception forward counts.  See the two prefix tuples
    for why the same test has to mean different things for the two rules.
    """
    prefixes = (
        _PRESERVING_RERAISE_PREFIXES if preserving_only else _RERAISE_PREFIXES
    )
    block = _enclosing_block(call["node"], body_node)
    for site in _call_sites(block, source_bytes):
        if site["kind"] != "name":
            continue
        if site["start_byte"] <= call["node"].end_byte:
            continue
        if site["name"].startswith(prefixes):
            return True
    return False


def _narrowed_between(
    func: dict, from_byte: int, to_byte: int, source_bytes: bytes
) -> bool:
    """True if a ``PyErr_ExceptionMatches``-style narrowing sits in a byte range.

    This is the gate that makes widening affordable: it is what separates a
    deliberate "recover from the expected error" from a blanket swallow, and it
    alone removes the idiomatic sentinel-lookup class the FP taxonomy warns
    about.
    """
    for site in _call_sites(func["body_node"], source_bytes):
        if site["kind"] != "name" or site["name"] not in _NARROWING_APIS:
            continue
        if from_byte <= site["start_byte"] < to_byte:
            return True
    return False


def _returns_pystatus(func: dict) -> bool:
    """Interpreter-init helpers convert to PyStatus; their clears are contractual."""
    return "PyStatus" in func.get("return_type", "")


def _check_non_destructor(
    func: dict, source_bytes: bytes, tree, assignments: dict
) -> list[dict]:
    """Apply the success-path and unfiltered-after-Python-call rules."""
    findings: list[dict] = []
    body_node = func["body_node"]

    # ``PyErr_Clear()`` in Python/errors.c is the public wrapper around
    # ``_PyErr_Clear(tstate)``; it is the implementation, not a use of it.
    if func["name"] in _CLEAR_NAMES:
        return findings

    for call in _find_pyerr_clear_calls(func, source_bytes):
        if is_suppressed_by_comment(source_bytes, tree, call["line"]):
            continue
        if _guard_brackets_clear(func, call, source_bytes):
            continue
        if _returns_pystatus(func):
            continue
        if _reports_exception_nearby(call, body_node, source_bytes):
            continue

        conditions = _enclosing_conditions(call["node"], body_node, source_bytes)
        signals = [
            (cond, _condition_signals(cond["node"], source_bytes, assignments))
            for cond in conditions
        ]
        # Guard clauses dominate the clear just as enclosing conditions do, but
        # they only participate in rule 2 — rule 3 needs the *enclosing* branch
        # to know which failure the clear is reacting to.
        dominating = list(signals) + [
            (cond, _condition_signals(cond["node"], source_bytes, assignments))
            for cond in _preceding_guard_conditions(
                call["node"], body_node, source_bytes
            )
        ]

        # --- Rule 2: nothing dominating the clear tests an error signal.
        if not any(s["errstate"] or s["calls"] for _, s in dominating):
            if _reraises_after(call, body_node, source_bytes):
                continue
            if _asserts_error_pending(call["node"], body_node, source_bytes):
                continue
            depth = len(conditions)
            findings.append(
                {
                    "type": "pyerr_clear_on_success_path",
                    "function": func["name"],
                    "line": call["line"],
                    "confidence": "high" if depth == 0 else "medium",
                    "enclosing_conditions": [c["text"] for c in conditions],
                    "detail": (
                        f"{call['name']}() in '{func['name']}' is not dominated by "
                        "any error test — no enclosing condition inspects "
                        "PyErr_Occurred / PyErr_ExceptionMatches or the failure of "
                        "a fallible call, so nothing this function did can be "
                        "pending here. The only exception it can discard is the "
                        "caller's (gh-146102). Fix: delete the clear and add "
                        "assert(!PyErr_Occurred()), as in "
                        "Objects/odictobject.c mutablemapping_add_pairs."
                    ),
                }
            )
            continue

        # --- Rule 3: the tested failure came from a call into user Python.
        # Only the *innermost* enclosing branch counts. Walking outward to find
        # some ancestor that happens to test a Python-reaching call attributes
        # the clear to a failure it is not reacting to; all eight Objects/
        # instances of this shape put the failing call in the innermost branch.
        if not signals:
            continue
        # A *bare* errstate probe (`if (PyErr_Occurred())`) names no failing
        # call of its own: it is a nested re-test of the same failure the next
        # condition out already tested, so `if (stop == -1) { if
        # (PyErr_Occurred()) PyErr_Clear(); }` must be attributed to the
        # `stop == -1` test, not dropped. Walk outward only while the condition
        # is that shape — an enclosing branch testing something *different*
        # (`if (module) { ... }`) still stops the walk, which is what keeps the
        # wrong-polarity-attribution FP class suppressed.
        pick = 0
        while (
            pick < len(signals)
            and not signals[pick][1]["calls"]
            and signals[pick][1]["errstate"]
        ):
            pick += 1
        if pick >= len(signals):
            continue
        cond, sig = signals[pick]
        if not sig["calls"]:
            continue
        in_else = cond["branch"] == "else"
        reaching = [
            c
            for c in sig["calls"]
            if _is_python_reaching(c["kind"], c["name"])
            and (c["then_failure"] != in_else)
        ]
        if not reaching:
            continue
        if _narrowed_between(
            func, cond["start_byte"], call["node"].start_byte, source_bytes
        ):
            continue
        if _reraises_after(
            call, body_node, source_bytes, preserving_only=True
        ):
            continue

        culprit = reaching[0]["name"]
        findings.append(
            {
                "type": "pyerr_clear_unfiltered_after_python_call",
                "function": func["name"],
                "line": call["line"],
                "confidence": "medium",
                "failing_call": culprit,
                "detail": (
                    f"{call['name']}() in '{func['name']}' clears the exception "
                    f"raised by '{culprit}' with no PyErr_ExceptionMatches "
                    "narrowing. That call dispatches into arbitrary Python "
                    "(__hash__ / __buffer__ / __index__ / import hooks), so its "
                    "failure return only means *something* was raised — a "
                    "MemoryError, KeyboardInterrupt or RecursionError is "
                    "discarded along with the expected TypeError. Narrow first, "
                    "e.g. if (!PyErr_ExceptionMatches(PyExc_TypeError)) return "
                    "NULL; before clearing (see Objects/genericaliasobject.c "
                    "set_orig_class)."
                ),
            }
        )
    return findings


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _dedupe_by_site(findings: list[dict]) -> list[dict]:
    """Collapse only exact ``(type, file, line)`` repeats.

    The shared ``deduplicate_findings`` keys on a *normalized detail* that
    erases quoted identifiers and line numbers, so two genuinely distinct
    clears in one file — ``bytearrayobject.c:1171`` and ``:1177``, both real —
    collapse into a single finding reported under one of the two functions.
    Every finding here is already one source line, so exact-site dedup is both
    sufficient and lossless.
    """
    seen: set[tuple[str, str, int]] = set()
    result: list[dict] = []
    for f in findings:
        key = (f.get("type", ""), f.get("file", ""), f.get("line", 0))
        if key in seen:
            continue
        seen.add(key)
        result.append(f)
    return result


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan for exception-clobbering PyErr_Clear() calls."""
    project_root, scan_root = resolve_roots(target)

    findings: list[dict] = []
    total_functions = 0
    total_clears = 0
    destructor_clears = 0
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
            clears = _find_pyerr_clear_calls(func, source_bytes)
            kind = _destructor_kind(func["name"], slot_map)
            if kind is not None:
                destructor_functions += 1
            if not clears:
                continue
            total_clears += len(clears)

            if kind is not None:
                destructor_clears += len(clears)
                new = _check_destructor(func, kind, source_bytes, tree)
            else:
                assignments = _collect_call_assignments(func["body_node"], source_bytes)
                new = _check_non_destructor(func, source_bytes, tree, assignments)

            for f in new:
                f["file"] = rel
                findings.append(f)

    findings = _dedupe_by_site(findings)

    by_confidence: dict[str, int] = defaultdict(int)
    by_slot: dict[str, int] = defaultdict(int)
    by_type: dict[str, int] = defaultdict(int)
    for f in findings:
        by_confidence[f["confidence"]] += 1
        by_type[f["type"]] += 1
        if "slot" in f:
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
            "by_type": dict(by_type),
        },
        destructor_functions=destructor_functions,
        total_pyerr_clear_calls=total_clears,
        total_pyerr_clear_calls_in_destructors=destructor_clears,
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

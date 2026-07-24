#!/usr/bin/env python3
"""Scan CPython C source for recursion-prone descents that lack a recursion guard.

The dangerous pattern (bug class A / R1): a function that descends into a
user-controlled Python object graph *without* ``Py_EnterRecursiveCall`` /
``Py_ReprEnter``. A deeply-nested or cyclic object then overflows the **C
stack** (SIGSEGV) instead of raising ``RecursionError``.

The organising fact — verified against CPython main @ 3.16.0a0 — is that the
four element-descent dispatchers are **not** equally dangerous:

===========================  ====================  =========================
dispatcher                   ``Objects/object.c``  wraps a recursion guard?
===========================  ====================  =========================
``PyObject_Repr``            :759                  yes
``PyObject_Str``             :800                  yes
``PyObject_RichCompare``     :1099                 yes
``PyObject_Hash``            :1158                 **NO**
===========================  ====================  =========================

So a ``tp_hash`` descent is unguarded at *every* level of the object graph and
is a genuine native SIGSEGV, while a repr/str/richcompare descent is normally
bounded by its dispatcher. The scanner therefore classifies by **element
operation**, not by slot:

* descent through ``PyObject_Hash`` -> ``missing_recursion_guard`` (high/medium)
* descent only through a guarded dispatcher ->
  ``recursion_descent_guarded_by_dispatcher`` (low, kept for auditability,
  excluded from the FIX count)

Descent shapes detected:

* **self-recursion** — the function calls itself with no guard (the
  ``_Py_make_parameters`` class, gh-154275). A guarded dispatcher does *not*
  help here: it increments the recursion counter once, then the self-call
  chain runs unbounded.
* **container element descent** — a loop over a container calling
  ``PyObject_Hash`` on its items (the ``tuple_hash`` /
  ``frozendict_pair_hash`` class, gh-154318 — noted upstream as a copy-pasted
  guardless algorithm).
* **field element descent** — a *fixed-arity* ``PyObject_Hash`` on a field of
  the receiver or on a local derived from it (``ga_hash``,
  ``weakref_hash_lock_held``, ``mappingproxy_hash``). Arity is irrelevant to
  depth: one field is enough, because the field is an arbitrary Python object.
  This shape has **no container/loop requirement**.
* **hash entry point** — ``PyObject_Hash`` on a plain function parameter. Adds
  exactly one C frame per call, so it is not additive per nesting level; it is
  reported at low confidence as an *entry point* into the unguarded hash graph
  unless a same-file recursion-prone slot calls it (the
  ``frozendict_hash`` -> ``frozendict_pair_hash`` shape), in which case it is
  additive after all.

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
    parse_bytes,
    walk_descendants,
)

# Map a function name -> slot from static type tables (designated + spec form).
# Recursion-prone slots (tp_hash / tp_richcompare / tp_repr / tp_str) descend
# the object graph on user-controlled input.
_SLOT_DESIGNATED_RE = re.compile(
    r"\.tp_(hash|richcompare|repr|str)\s*=\s*(?:\(\s*\w[\w\s\*]*\)\s*)?(?:&\s*)?(\w+)"
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

# CPython's free-threading refactor made "<slot>_lock_held" a pervasive idiom:
# the registered slot is a three-line critical-section wrapper and the real
# descent lives in the helper, whose name matches no slot suffix. Strip these
# before slot classification so the helper is still classified by its slot.
_DELEGATION_SUFFIXES: tuple[str, ...] = (
    "_lock_held",
    "_lockheld",
    "_LockHeld",
    "_locked",
    "_unlocked",
    "_impl",
)

# The parameter-walk class (PEP 585 generic alias): recurses over nested
# type arguments; not a slot, but the same native-stack-overflow shape.
_PARAM_WALK_RE = re.compile(r"(?:make|subs)_parameters", re.IGNORECASE)

# The one element-descent dispatcher with NO recursion guard (object.c:1158).
# Every call to it is an unguarded entry into the object graph.
_UNGUARDED_DISPATCH = frozenset({"PyObject_Hash"})

# Element-descent dispatchers that DO wrap _Py_EnterRecursiveCallTstate. A
# descent that only goes through these is bounded by the interpreter recursion
# limit, so it raises RecursionError rather than segfaulting.
_GUARDED_DISPATCH = frozenset(
    {
        "PyObject_Repr",
        "PyObject_Str",
        "PyObject_ASCII",
        "PyObject_RichCompare",
        "PyObject_RichCompareBool",
    }
)

# Where each guarded dispatcher's guard lives (CPython main @ 3.16.0a0).
_DISPATCH_GUARD_SITE: dict[str, str] = {
    "PyObject_Repr": "Objects/object.c:759",
    "PyObject_Str": "Objects/object.c:800",
    "PyObject_ASCII": "Objects/object.c:759 (via PyObject_Repr)",
    "PyObject_RichCompare": "Objects/object.c:1099",
    "PyObject_RichCompareBool": "Objects/object.c:1099 (via PyObject_RichCompare)",
}

# Container-iteration signals (used to tell a per-element descent apart from a
# single bounded field access).
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

# Slots whose descent, when unguarded, is additive per nesting level.
_RECURSION_PRONE_CALLER_SLOTS = frozenset({"tp_hash", "parameter_walk"})

_CAST_PREFIX_RE = re.compile(
    r"^\(\s*(?:const\s+|struct\s+|unsigned\s+)*\w[\w\s\*]*\)\s*"
)
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")

# Constructors of *provably non-nestable* objects. Hashing a freshly built
# int / float / str / bytes / bool can never re-enter the object graph, so the
# descent bound is exactly 0 and the site is not reported at all
# (``channelid_hash``: ``PyObject_Hash(PyLong_FromLongLong(...))``).
_SCALAR_CTOR_RE = re.compile(
    r"^_?Py(?:Long|Float|Bool|Complex|Unicode|Bytes|ByteArray)_[A-Za-z_]*\s*\("
)

# Constructors of a *fresh container* built inside the function from receiver
# fields (``range_hash``, ``delta_hash``-style state tuples). One level of
# descent, into whatever the fields happen to be -- report, but at reduced
# confidence, because the bound is the nestability of those fields.
_TEMP_CTOR_RE = re.compile(
    r"^_?Py(?:Tuple|List|FrozenSet|Set|Dict)_(?:New|Pack)\s*\(|^Py_BuildValue\s*\("
)

# A pure lvalue path: `self`, `self->view`, `a->b.c`, `items[i]`.
_LVALUE_PATH_RE = re.compile(
    r"[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*|\s*\[[^\]]*\])*"
)
# CPython's slot-receiver downcast idiom: `PyDelta_CAST(op)`.
_CAST_MACRO_RE = re.compile(r"^_?\w+_(?:CAST|Cast|cast)\s*\((.*)\)$", re.DOTALL)


def _collect_slot_map(source: str) -> dict[str, str]:
    slot_map: dict[str, str] = {}
    for slot, name in _SLOT_DESIGNATED_RE.findall(source):
        slot_map[name] = f"tp_{slot}"
    for slot, name in _SLOT_SPEC_RE.findall(source):
        slot_map[name] = f"tp_{slot}"
    return slot_map


def strip_delegation_suffix(func_name: str) -> str:
    """Strip a free-threading delegation suffix (``foo_hash_lock_held``).

    Returns the name unchanged when no suffix applies.
    """
    for suffix in _DELEGATION_SUFFIXES:
        if func_name.endswith(suffix) and len(func_name) > len(suffix):
            return func_name[: -len(suffix)]
    return func_name


def _slot_for(func_name: str, slot_map: dict[str, str]) -> str | None:
    """Classify a function as a recursion-prone slot, else None."""
    if func_name in slot_map:
        return slot_map[func_name]
    if _PARAM_WALK_RE.search(func_name):
        return "parameter_walk"
    base = strip_delegation_suffix(func_name)
    if base != func_name and base in slot_map:
        return slot_map[base]
    for candidate in (func_name, base):
        for suffix, slot in _SLOT_NAME_SUFFIX:
            if candidate.endswith(suffix):
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


def _split_top_level(text: str) -> list[str]:
    depth = 0
    current: list[str] = []
    parts: list[str] = []
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def parameter_names(params_text: str) -> list[str]:
    """Extract the declared parameter names, in declaration order."""
    names: list[str] = []
    for part in _split_top_level(params_text):
        idents = _IDENT_RE.findall(part)
        if idents and idents[-1] not in ("void",):
            names.append(idents[-1])
    return names


def _strip_casts(text: str) -> str:
    prev = None
    out = text.strip()
    while out != prev:
        prev = out
        out = _CAST_PREFIX_RE.sub("", out).strip()
        if out.startswith("(") and out.endswith(")"):
            inner = out[1:-1]
            if _split_top_level(inner) == [inner]:
                out = inner.strip()
    return out


def _base_identifier(expr: str) -> str | None:
    """The root identifier of an lvalue-ish expression (``a->b[i]`` -> ``a``)."""
    m = _IDENT_RE.match(_strip_casts(expr))
    return m.group(0) if m else None


def receiver_roots(func: dict, slot: str | None, source_bytes: bytes) -> set[str]:
    """Names that denote *this object* inside ``func``.

    Seeded with the first parameter — but only for a function classified as a
    recursion-prone slot, where the first parameter really is the receiver.
    A standalone helper such as ``hashtable_hash_pyobject(const void *key)``
    gets an empty seed, so hashing ``key`` stays a caller-supplied
    pass-through rather than a receiver descent.

    Then propagated through local assignments: ``gaobject *alias =
    (gaobject *)self;`` makes ``alias`` a receiver root too.
    """
    params = parameter_names(func["parameters"])
    roots: set[str] = set()
    if slot is not None and params:
        roots.add(params[0])
    if not roots:
        return roots

    assignments = find_assignments_in_scope(func["body_node"], source_bytes)
    for _ in range(4):
        changed = False
        for a in assignments:
            var = a["variable"]
            if not _IDENT_RE.fullmatch(var) or var in roots:
                continue
            if _aliases_receiver(a["value_text"], roots):
                roots.add(var)
                changed = True
        if not changed:
            break
    return roots


def _aliases_receiver(rhs: str, roots: set[str]) -> bool:
    """True when ``rhs`` is *the receiver itself*, not merely derived from it.

    Only two forms alias: a (possibly cast, possibly address-of) lvalue path
    rooted at a receiver root — ``(gaobject *)self``, ``&self->master`` — and a
    ``*_CAST(...)`` downcast macro, which CPython uses pervasively for slot
    receivers (``PyDelta_CAST(op)``, ``_PyMethodObject_CAST(self)``).

    Deliberately *not* "mentions a root anywhere": ``PyLong_FromLongLong(
    cidobj->cid)`` builds a fresh int out of a receiver field and must not
    make its result a receiver alias, or ``channelid_hash`` looks like an
    unbounded descent when its bound is 0.
    """
    expr = _strip_casts(rhs).lstrip("&").strip()
    m = _CAST_MACRO_RE.match(expr)
    if m:
        expr = _strip_casts(m.group(1)).lstrip("&").strip()
    if not _LVALUE_PATH_RE.fullmatch(expr):
        return False
    base = _base_identifier(expr)
    return base in roots


def classify_hash_argument(
    arg_text: str,
    params: list[str],
    roots: set[str],
    assignments: dict[str, list[str]],
    _depth: int = 0,
) -> str:
    """Classify the hashed expression by where its *depth* comes from.

    Returns one of:

    ``"receiver"``
        A field of the receiver, or a local derived from one. Additive per
        nesting level -> the real SIGSEGV shape (``ga_hash``, ``union_hash``,
        ``weakref_hash_lock_held``).
    ``"container"``
        An element pulled out of a container the function owns
        (``*_GET_ITEM``, subscript). Also additive (``tuple_hash``).
    ``"temporary"``
        A container freshly built inside the function. One level of descent,
        bounded by the nestability of whatever was packed into it.
    ``"scalar"``
        A freshly built int/str/bytes/... — bound is exactly 0, not a finding.
    ``"parameter"``
        A caller-supplied argument. Adds exactly one C frame; the depth
        belongs to the caller.
    ``"unknown"``
        Origin not resolvable syntactically.
    """
    arg = _strip_casts(arg_text)
    if not arg:
        return "unknown"
    base = _base_identifier(arg)

    if _SCALAR_CTOR_RE.match(arg):
        return "scalar"
    if _TEMP_CTOR_RE.match(arg):
        return "temporary"

    if _IDENT_RE.fullmatch(arg) and arg in roots:
        # Hashing the whole receiver is a pass-through, not a descent into a
        # sub-object: `hamt_hash(PyObject *o) { PyObject_Hash(o); }`.
        return "parameter"
    if base in roots:
        if _CONTAINER_ACCESS_RE.search(arg) or "[" in arg:
            return "container"
        return "receiver"
    if _CONTAINER_ACCESS_RE.search(arg) or "[" in arg:
        # Element of *something*; additive only if that something is ours.
        inner = _IDENT_RE.findall(arg)
        if roots & set(inner):
            return "container"
        if set(params) & set(inner):
            return "parameter"
        return "unknown"
    if base in params:
        return "parameter"

    if base is not None and _IDENT_RE.fullmatch(arg) and _depth < 3:
        # A bare local: follow its assignment(s).
        kinds = {
            classify_hash_argument(rhs, params, roots, assignments, _depth + 1)
            for rhs in assignments.get(arg, ())
        }
        for preferred in ("receiver", "container", "temporary", "parameter", "scalar"):
            if preferred in kinds:
                return preferred
        return "unknown"

    idents = set(_IDENT_RE.findall(arg))
    if idents & roots:
        return "receiver"
    if idents & set(params):
        return "parameter"
    return "unknown"


_CONSTANT_RE = re.compile(r"[A-Z_][A-Z0-9_]*|Py_\w+|-?\d+[uUlL]*")


def self_call_descends(args_text: str, params: list[str], roots: set[str]) -> bool:
    """True when a self-call actually descends into a *new* object.

    ``ga_richcompare(a, b, Py_EQ)`` / ``set_richcompare((PyObject *)v, w,
    Py_EQ)`` re-enter with the same operands and a different opcode: bounded at
    one extra frame, not a descent. ``_Py_make_parameters(t)`` passes a loop
    element, and ``_Py_subs_parameters(self, arg, parameters, item)`` passes a
    container element — those are real descents.
    """
    args = args_text.strip()
    if not args:
        return True
    for arg in _split_top_level(args):
        expr = _strip_casts(arg).lstrip("&").strip()
        if not expr:
            continue
        if _CONSTANT_RE.fullmatch(expr):
            continue
        if _IDENT_RE.fullmatch(expr) and (expr in params or expr in roots):
            continue
        return True
    return False


_LOOP_NODE_TYPES = frozenset({"for_statement", "while_statement", "do_statement"})


def _in_loop(call_node) -> bool:
    """True when the call sits inside a for/while/do body."""
    node = call_node.parent
    while node is not None and node.type != "function_definition":
        if node.type in _LOOP_NODE_TYPES:
            return True
        node = node.parent
    return False


def _is_tail_call(call_node) -> bool:
    """True when the call is the whole of a ``return <call>;`` statement."""
    parent = call_node.parent
    while parent is not None and parent.type in ("parenthesized_expression",):
        parent = parent.parent
    return parent is not None and parent.type == "return_statement"


def _first_argument(arguments_text: str) -> str:
    depth = 0
    for i, ch in enumerate(arguments_text):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            return arguments_text[:i]
    return arguments_text


def _analyze_function(
    func: dict,
    slot: str | None,
    calls: list[dict],
    slot_callers: set[str],
    source_bytes: bytes,
    tree,
) -> dict | None:
    """Return a finding dict for an unguarded recursive descent, else None."""
    if _has_guard(func):
        return None

    call_names = {c["function_name"] for c in calls}
    params = parameter_names(func["parameters"])
    roots = receiver_roots(func, slot, source_bytes)
    assignments: dict[str, list[str]] = defaultdict(list)
    for a in find_assignments_in_scope(func["body_node"], source_bytes):
        if _IDENT_RE.fullmatch(a["variable"]):
            assignments[a["variable"]].append(a["value_text"])

    # ---- shape 1: self-recursion (unaffected by any dispatcher guard) ------
    descending_self_calls = [
        c
        for c in calls
        if c["function_name"] == func["name"]
        and self_call_descends(c["arguments_text"], params, roots)
    ]
    if slot is not None and descending_self_calls:
        line = descending_self_calls[0]["start_line"]
        if is_suppressed_by_comment(source_bytes, tree, line):
            return None
        return {
            "type": "missing_recursion_guard",
            "function": func["name"],
            "slot": slot,
            "shape": "self_recursion",
            "element_op": func["name"],
            "line": line,
            "confidence": "high",
            "detail": (
                f"'{func['name']}' recurses into itself with no "
                "Py_EnterRecursiveCall guard — a deeply-nested or cyclic object "
                "overflows the C stack (SIGSEGV) instead of raising "
                "RecursionError. A guarded dispatcher does not help: it "
                "increments the recursion counter once, then the self-call "
                "chain runs unbounded. Bracket the descent with "
                "Py_EnterRecursiveCall()/Py_LeaveRecursiveCall()."
            ),
        }

    # ---- shape 2: descent through the UNGUARDED dispatcher (PyObject_Hash) --
    hash_calls = [c for c in calls if c["function_name"] in _UNGUARDED_DISPATCH]
    if hash_calls:
        sites = []
        for c in hash_calls:
            kind = classify_hash_argument(
                _first_argument(c["arguments_text"]), params, roots, assignments
            )
            if kind == "receiver" and _in_loop(c["node"]):
                # A receiver-derived value hashed once per loop iteration is a
                # per-element container descent, whatever the accessor spelling.
                kind = "container"
            sites.append(
                {
                    "line": c["start_line"],
                    "argument_kind": kind,
                    "argument": _first_argument(c["arguments_text"]).strip(),
                    "node": c["node"],
                }
            )

        # A descent into a freshly built scalar (int/str/bytes) can never
        # re-enter the object graph: bound is exactly 0. Drop those sites.
        sites = [s for s in sites if s["argument_kind"] != "scalar"]
        if not sites:
            return None

        # Rank by how the depth is supplied. `receiver` first so union_hash
        # reports its unbounded tail (`PyObject_Hash(alias->hashable_args)`)
        # rather than the bounded error-path loop above it.
        rank = {
            "receiver": 0,
            "container": 1,
            "temporary": 2,
            "parameter": 3,
            "unknown": 4,
        }
        primary = min(sites, key=lambda s: (rank[s["argument_kind"]], s["line"]))
        line = primary["line"]
        kind = primary["argument_kind"]

        if is_suppressed_by_comment(source_bytes, tree, line):
            return None

        tail_call = len(sites) == 1 and _is_tail_call(primary["node"])
        caller_is_slot = bool(slot_callers)

        if kind in ("receiver", "container"):
            shape = (
                "container_element_descent"
                if kind == "container"
                else "field_element_descent"
            )
            confidence = "high"
            why = f"'{func['name']}' calls PyObject_Hash on " + (
                "each element of a container it owns"
                if kind == "container"
                else "a receiver-derived value (fixed arity — arity is "
                "irrelevant to depth, the value is an arbitrary Python "
                "object)"
            )
        elif kind == "temporary":
            shape = "temporary_container_descent"
            confidence = "medium"
            why = (
                f"'{func['name']}' hashes a container it builds locally from "
                "receiver fields — one level of descent, bounded by whether "
                "those fields can themselves nest"
            )
        elif caller_is_slot:
            shape = "slot_helper_descent"
            confidence = "high"
            why = (
                f"'{func['name']}' calls PyObject_Hash on a parameter and is "
                f"called from recursion-prone slot(s) "
                f"{', '.join(sorted(slot_callers))} in the same file, so the "
                "descent is additive per nesting level"
            )
        else:
            shape = "hash_entry_point"
            confidence = "low"
            why = (
                f"'{func['name']}' calls PyObject_Hash on a caller-supplied "
                "value — this adds exactly one C frame, so it is an entry "
                "point into the unguarded hash graph rather than an additive "
                "descent"
            )

        if tail_call and confidence == "high":
            # A bare `return PyObject_Hash(x);` is routinely turned into a jump
            # by an optimising compiler, so the crash may not reproduce on a
            # release build (mappingproxy_hash survives 3M levels under clang
            # -O). Report it honestly rather than as a reproducible crash.
            confidence = "medium"

        return {
            "type": "missing_recursion_guard",
            "function": func["name"],
            "slot": slot or "not_a_slot",
            "shape": shape,
            "element_op": "PyObject_Hash",
            "argument_kind": kind,
            "tail_call": tail_call,
            "line": line,
            "sites": [
                {k: v for k, v in s.items() if k != "node"}
                for s in sorted(sites, key=lambda s: s["line"])
            ],
            "confidence": confidence,
            "detail": (
                f"{why}. PyObject_Hash (Objects/object.c:1158) is the one "
                "element-descent dispatcher with NO recursion guard, so a "
                "tp_hash descent is unguarded at every level: a deeply-nested "
                "or cyclic object overflows the C stack (SIGSEGV) instead of "
                "raising RecursionError. Bracket the descent with "
                'Py_EnterRecursiveCall(" while hashing")/'
                "Py_LeaveRecursiveCall()."
                + (
                    " Note: the descent is a bare tail call, which an "
                    "optimising compiler may eliminate — confirm on -O0 before "
                    "claiming a crash."
                    if tail_call
                    else ""
                )
            ),
        }

    # ---- shape 3: descent only through a GUARDED dispatcher ---------------
    guarded_apis = call_names & _GUARDED_DISPATCH
    if not guarded_apis or slot is None:
        return None
    has_container = bool(_CONTAINER_ACCESS_RE.search(func["body"])) or _has_loop(
        func["body_node"]
    )
    if not has_container:
        return None

    line = next(
        (c["start_line"] for c in calls if c["function_name"] in guarded_apis),
        func["start_line"],
    )
    if is_suppressed_by_comment(source_bytes, tree, line):
        return None

    guard_sites = sorted({_DISPATCH_GUARD_SITE[a] for a in guarded_apis})
    return {
        "type": "recursion_descent_guarded_by_dispatcher",
        "function": func["name"],
        "slot": slot,
        "shape": "guarded_dispatcher_descent",
        "element_op": ", ".join(sorted(guarded_apis)),
        "guarded_by": guard_sites,
        "line": line,
        "confidence": "low",
        "detail": (
            f"'{func['name']}' ({slot}) descends into elements via "
            f"{', '.join(sorted(guarded_apis))}, which wrap "
            f"_Py_EnterRecursiveCallTstate at {', '.join(guard_sites)}. The "
            "descent is therefore bounded by the interpreter recursion limit "
            "and raises RecursionError, not SIGSEGV. Reported for "
            "auditability only — promote to a real finding only if you can "
            "show the slot is reached by a route that bypasses its dispatcher."
        ),
    }


def _dedupe(findings: list[dict]) -> list[dict]:
    """Drop exact repeats keyed by (type, file, function, line).

    Deliberately *not* ``scan_common.deduplicate_findings``: that helper
    normalises quoted identifiers out of the detail text, which collapsed the
    two distinct parameter-walk self-recursions (``_Py_make_parameters`` and
    ``_Py_subs_parameters``, both in ``genericaliasobject.c``) into a single
    finding and hid the second one.
    """
    seen: set[tuple[str, str, str, int]] = set()
    result: list[dict] = []
    for f in findings:
        key = (f["type"], f.get("file", ""), f["function"], f["line"])
        if key in seen:
            continue
        seen.add(key)
        result.append(f)
    return result


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan for recursion-prone descents lacking a recursion guard."""
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

        # Pass 1: per-function calls + slot classification, and the same-file
        # callee -> recursion-prone-slot-callers map (the frozendict_hash ->
        # frozendict_pair_hash shape).
        calls_by_func: list[list[dict]] = []
        slots: list[str | None] = []
        slot_callers: dict[str, set[str]] = defaultdict(set)
        for func in functions:
            calls = find_calls_in_scope(func["body_node"], source_bytes)
            calls_by_func.append(calls)
            slot = _slot_for(func["name"], slot_map)
            slots.append(slot)
            if slot in _RECURSION_PRONE_CALLER_SLOTS:
                for c in calls:
                    if c["function_name"] != func["name"]:
                        slot_callers[c["function_name"]].add(func["name"])

        # Pass 2: analyse.
        for func, calls, slot in zip(functions, calls_by_func, slots):
            total_functions += 1
            if slot is not None:
                slot_functions += 1
            f = _analyze_function(
                func,
                slot,
                calls,
                slot_callers.get(func["name"], set()),
                source_bytes,
                tree,
            )
            if f is not None:
                f["file"] = rel
                findings.append(f)

    findings = _dedupe(findings)

    by_confidence: dict[str, int] = defaultdict(int)
    by_shape: dict[str, int] = defaultdict(int)
    by_type: dict[str, int] = defaultdict(int)
    for f in findings:
        by_confidence[f["confidence"]] += 1
        by_shape[f["shape"]] += 1
        by_type[f["type"]] += 1

    unguarded = [f for f in findings if f["type"] == "missing_recursion_guard"]

    return build_report(
        project_root=project_root,
        scan_root=scan_root,
        files_analyzed=files_analyzed,
        functions_analyzed=total_functions,
        findings=findings,
        summary={
            "total_findings": len(findings),
            "missing_recursion_guard": len(unguarded),
            "guarded_by_dispatcher": len(findings) - len(unguarded),
            "by_confidence": dict(by_confidence),
            "by_shape": dict(by_shape),
            "by_type": dict(by_type),
        },
        recursion_prone_slot_functions=slot_functions,
        dispatcher_guard_model={
            "unguarded": {"PyObject_Hash": "Objects/object.c:1158 — NO guard"},
            "guarded": _DISPATCH_GUARD_SITE,
            "note": (
                "Verified against CPython main @ 3.16.0a0. Only PyObject_Hash "
                "lacks _Py_EnterRecursiveCallTstate, so only hash descents are "
                "unguarded at every level. Self-recursion is unguarded "
                "regardless of dispatcher."
            ),
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

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
  chain runs unbounded. Reported for *any* function, not only a recognised
  slot: what slot-hood stood in for -- "the recursion follows user-controlled
  data" -- is now stated directly, by requiring the recursive argument to come
  out of an **element operation** (a container extractor, a class-hierarchy
  walk, or a hierarchy attribute lookup). Gating on slot-hood gave precision
  0/1 and recall 0/7 on ``Objects/typeobject.c``, whose seven descents are all
  non-slot ``static`` helpers -- among them a reproduced SIGSEGV reachable
  through the builtin ``dir()``.
* **mutual recursion** — a cycle of two or more same-file functions in which
  *none* calls itself, so per-function analysis structurally cannot see the
  recursion at all (``update_subclasses`` <-> ``recurse_down_subclasses``, an
  ASan-confirmed native stack overflow). Found from the same intra-file call
  graph ``scan_stw_safety`` builds. A guard -- or a hand-rolled depth bound
  against a constant, as ``Python/marshal.c`` uses -- anywhere in the cycle
  discharges all of it.
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

# Element-descent dispatchers with NO recursion guard. Every call to one of
# these is an unguarded entry into the object graph.
#
# `PyObject_Hash` (Objects/object.c:1158) is the primary spelling.
# `_PyObject_HashDictKey` (Include/internal/pycore_object.h:840) is the same
# operation under another name: a `static inline Py_ALWAYS_INLINE` wrapper with
# a PyUnicode_CheckExact cached-hash fast path whose tail is literally
# `return PyObject_Hash(op);`. Omitting it hid 27 call sites tree-wide,
# including 8+ in Objects/dictobject.c and Modules/_collectionsmodule.c:2592
# (`collections.Counter` -> confirmed stack overflow under ASan).
_UNGUARDED_DISPATCH = frozenset({"PyObject_Hash", "_PyObject_HashDictKey"})

# Hash spellings whose bound is exactly 0 — they never descend into the object
# graph, so they are excluded *deliberately* and with a reason, rather than by
# never having been in the vocabulary. `PyObject_GenericHash` hashes the
# object's identity (Objects/object.c), `Py_HashPointer` a raw address, and
# `Py_HashBuffer` a flat byte range.
_BOUND_ZERO_HASH = frozenset(
    {"PyObject_GenericHash", "Py_HashPointer", "Py_HashBuffer"}
)

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

# ---------------------------------------------------------------------------
# Element operations: where a recursive call's argument comes from
# ---------------------------------------------------------------------------
#
# Self-recursion used to be reported only for a function classified as a
# recursion-prone *slot*.  All seven recursive descents in
# ``Objects/typeobject.c`` are non-slot ``static`` helpers, so the shape was
# computed and then discarded: precision 0/1, recall 0/7 on a file holding a
# reproduced SIGSEGV (``merge_class_dict``, CPY-0071, reachable through the
# builtin ``dir()``) and an ASan-confirmed overflow (CPY-0087).
#
# Slot-hood was standing in for "the recursion follows user-controlled data".
# Naming that directly is both broader and tighter: the recursive argument must
# come out of an element operation.

# Container element extraction.
_CONTAINER_ELEMENT_OPS = frozenset(
    {
        "PyTuple_GET_ITEM",
        "PyTuple_GetItem",
        "PyList_GET_ITEM",
        "PyList_GetItem",
        "PySequence_GetItem",
        "PySequence_Fast_GET_ITEM",
        "PySequence_ITEM",
        "PyDict_Next",
        "PyDict_GetItem",
        "PyDict_GetItemRef",
        "PyDict_GetItemWithError",
        "PyObject_GetItem",
        "PyIter_Next",
        "PyStructSequence_GET_ITEM",
    }
)

# Class-hierarchy walks.  None of the seven typeobject.c descents calls an
# element-descent *dispatcher* at all -- they walk ``__bases__`` /
# ``__mro__`` / ``tp_subclasses``, which is a Python-mutable graph of exactly
# the same unbounded depth.  Without these the rule stays blind on the file
# that motivated it.
_OBJECT_GRAPH_WALK_OPS = frozenset(
    {
        "lookup_tp_bases",
        "lookup_tp_mro",
        "lookup_tp_subclasses",
        "_PyType_GetBases",
        "_PyType_GetMRO",
        "_PyType_GetSubclasses",
        "type_from_ref",
    }
)

# Attribute lookups that fetch the hierarchy by name, for the ``dir()`` path
# that does not have a PyTypeObject to hand.
_ATTR_LOOKUP_OPS = frozenset(
    {
        "PyObject_GetAttr",
        "PyObject_GetAttrString",
        "PyObject_GetOptionalAttr",
        "PyObject_GetOptionalAttrString",
        "_PyObject_LookupAttr",
    }
)

_ELEMENT_OPS = _CONTAINER_ELEMENT_OPS | _OBJECT_GRAPH_WALK_OPS | _ATTR_LOOKUP_OPS

# ``&_Py_ID(__bases__)`` in an attribute lookup names the hierarchy explicitly.
_GRAPH_ATTR_IDS = frozenset({"__bases__", "__mro__", "__subclasses__", "__class__"})
_PY_ID_RE = re.compile(r"_Py_ID\s*\(\s*(\w+)\s*\)")

# ``&out`` in an argument list: the callee writes the element through it, which
# is how PyDict_Next hands back the value it just walked to.
_ADDRESS_OF_ARG_RE = re.compile(r"^&\s*([A-Za-z_]\w*)$")

_MAX_DERIVATION_ROUNDS = 6

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

# `Py_BuildValue("iii", ...)` packs three C ints: the result is a container, but
# every leaf is a freshly built scalar, so the descent bound is 0 exactly as for
# `_SCALAR_CTOR_RE`. Only the object-taking codes (O S N U V) can introduce a
# nestable element.
_BUILDVALUE_FIRST_ARG_RE = re.compile(r'^Py_BuildValue\s*\(\s*"((?:[^"\\]|\\.)*)"')
_BUILDVALUE_OBJECT_CODES = frozenset("OSNUV")

# A call expression whose callee is a plain identifier: `delta_getstate(self)`.
_CALL_EXPR_RE = re.compile(r"^([A-Za-z_]\w*)\s*\((.*)\)$", re.DOTALL)
# A file-local helper is only followed if it is a thin single-`return` wrapper.
_MAX_ONE_HOP_STATEMENTS = 5

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


# A hand-rolled depth bound: `if (p->depth > MAX_MARSHAL_STACK_DEPTH)`.  It is
# not `Py_EnterRecursiveCall`, but it bounds the recursion just as well, and
# ``Python/marshal.c`` -- a genuinely user-data-driven recursive descent -- uses
# exactly this.  Recognising it is the difference between reporting marshal's
# cycle and reporting only the unbounded ones.
#
# Requires the counter to be compared against a *constant* (an uppercase macro
# or a literal), not another variable: `if (depth > n)` inside a loop is an
# index test, not a recursion bound.
_DEPTH_BOUND_RE = re.compile(
    r"\b\w*(?:depth|nesting|recursion)\w*\s*(?:>=?|<=?)\s*"
    r"(?:[A-Z_][A-Z0-9_]{3,}|\d+)"
    r"|(?:[A-Z_][A-Z0-9_]{3,}|\d+)\s*(?:>=?|<=?)\s*\b\w*(?:depth|nesting|recursion)\w*",
    re.IGNORECASE | re.VERBOSE,
)


def _has_depth_bound(func: dict) -> bool:
    """True when the body enforces its own depth limit against a constant."""
    return bool(_DEPTH_BOUND_RE.search(func["body"]))


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


def _buildvalue_is_scalar(arg: str) -> bool:
    """True for a ``Py_BuildValue`` whose format packs only non-nestable leaves."""
    m = _BUILDVALUE_FIRST_ARG_RE.match(arg)
    if m is None:
        return False
    fmt = m.group(1)
    return bool(fmt) and not (set(fmt) & _BUILDVALUE_OBJECT_CODES)


def ctor_returning_helpers(functions: list[dict]) -> dict[str, str]:
    """Map file-local thin helper -> ``"scalar"`` / ``"temporary"``.

    The ``*_getstate`` idiom (``delta_getstate`` is
    ``return Py_BuildValue("iii", GET_TD_DAYS(self), ...);``) hides the fresh
    container behind one call, so ``_TEMP_CTOR_RE`` — which only matches a
    constructor written in the argument expression itself — degraded
    ``delta_hash`` to ``field_element_descent`` at ``high``, the worst-rated
    finding in the informed ``Modules/`` sample.  Following exactly one hop into
    a helper whose whole body is ``return <ctor>(...)`` fixes that, and
    generalises to the whole ``*_getstate`` family.

    Deliberately conservative: single-``return`` bodies only, under
    ``_MAX_ONE_HOP_STATEMENTS`` statements, never across files, and only when
    the returned expression is a constructor — so the classification never
    depends on the callee's own receiver/parameter scope.
    """
    out: dict[str, str] = {}
    for func in functions:
        body = func.get("body", "")
        if body.count(";") > _MAX_ONE_HOP_STATEMENTS:
            continue
        returns = re.findall(r"\breturn\s+([^;]+);", body)
        if len(returns) != 1:
            continue
        expr = _strip_casts(returns[0].strip())
        if _SCALAR_CTOR_RE.match(expr) or _buildvalue_is_scalar(expr):
            out[func["name"]] = "scalar"
        elif _TEMP_CTOR_RE.match(expr):
            out[func["name"]] = "temporary"
    return out


def classify_hash_argument(
    arg_text: str,
    params: list[str],
    roots: set[str],
    assignments: dict[str, list[str]],
    _depth: int = 0,
    ctor_helpers: dict[str, str] | None = None,
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

    if _SCALAR_CTOR_RE.match(arg) or _buildvalue_is_scalar(arg):
        return "scalar"
    if _TEMP_CTOR_RE.match(arg):
        return "temporary"

    # One hop into a file-local `return <ctor>(...)` helper.
    if ctor_helpers and _depth < 3:
        call_m = _CALL_EXPR_RE.match(arg)
        if call_m is not None and call_m.group(1) in ctor_helpers:
            return ctor_helpers[call_m.group(1)]

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
            classify_hash_argument(
                rhs, params, roots, assignments, _depth + 1, ctor_helpers
            )
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


def element_derived_names(
    calls: list[dict], assignments: dict[str, list[str]]
) -> dict[str, str]:
    """Map local name -> the element operation its value came out of.

    Three binding forms, all present in the ``Objects/typeobject.c`` descents:

    * a plain assignment -- ``PyObject *b = PyTuple_GET_ITEM(bases, i);``
      (``assign_version_tag``);
    * an **out-parameter** -- ``while (PyDict_Next(subclasses, &i, NULL, &ref))``
      binds ``ref`` with no assignment node at all
      (``_PyType_Modified_Unlocked``);
    * one further hop -- ``subclass = type_from_ref(ref);``, where the element
      arrives through a helper rather than directly.

    Only the *hierarchy* attribute names count for an attribute lookup:
    ``PyObject_GetOptionalAttr(aclass, &_Py_ID(__bases__), &bases)`` is a graph
    walk, an arbitrary ``getattr`` is not.
    """
    derived: dict[str, str] = {}

    for c in calls:
        name = c["function_name"]
        args = c.get("arguments_text", "")
        if name in _ATTR_LOOKUP_OPS:
            ids = {m.group(1) for m in _PY_ID_RE.finditer(args)}
            if not (ids & _GRAPH_ATTR_IDS):
                continue
        elif name not in _ELEMENT_OPS:
            continue
        for arg in _split_top_level(args):
            m = _ADDRESS_OF_ARG_RE.match(arg.strip())
            if m:
                derived.setdefault(m.group(1), name)

    for var, rhs_list in assignments.items():
        for rhs in rhs_list:
            for op in _ELEMENT_OPS:
                if re.search(rf"\b{re.escape(op)}\s*\(", rhs):
                    derived.setdefault(var, op)
                    break

    for _ in range(_MAX_DERIVATION_ROUNDS):
        changed = False
        for var, rhs_list in assignments.items():
            if var in derived:
                continue
            for rhs in rhs_list:
                hit = next(
                    (
                        derived[i]
                        for i in _IDENT_RE.findall(rhs)
                        if i in derived and i != var
                    ),
                    None,
                )
                if hit is not None:
                    derived[var] = hit
                    changed = True
                    break
        if not changed:
            break
    return derived


def descent_element_op(args_text: str, derived: dict[str, str]) -> str | None:
    """The element op a recursive call's arguments come from, if any.

    Every identifier in each argument is considered, not just the base one:
    the descents spell it ``_PyType_CAST(b)`` and ``(PyTypeObject *)child`` as
    often as they spell it ``subclass``.
    """
    for arg in _split_top_level(args_text):
        expr = _strip_casts(arg).lstrip("&").strip()
        for ident in _IDENT_RE.findall(expr):
            if ident in derived:
                return derived[ident]
        for op in _ELEMENT_OPS:
            if re.search(rf"\b{re.escape(op)}\s*\(", arg):
                return op
    return None


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
    ctor_helpers: dict[str, str] | None = None,
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
    # Slot-hood is no longer required.  What it stood in for -- "the recursion
    # follows user-controlled data" -- is now stated directly: the recursive
    # argument must come out of an element operation.  Gating on slot-hood gave
    # precision 0/1 and recall 0/7 on Objects/typeobject.c, whose seven
    # descents are all non-slot static helpers.
    derived = element_derived_names(calls, assignments)
    element_op: str | None = None
    for c in descending_self_calls:
        element_op = descent_element_op(c["arguments_text"], derived)
        if element_op is not None:
            break
    if descending_self_calls and (slot is not None or element_op is not None):
        line = descending_self_calls[0]["start_line"]
        if is_suppressed_by_comment(source_bytes, tree, line):
            return None
        graph = element_op in _OBJECT_GRAPH_WALK_OPS or element_op in _ATTR_LOOKUP_OPS
        via = (
            f" The recursive argument comes out of {element_op}, so the depth is "
            + (
                "the depth of a Python-mutable class hierarchy"
                if graph
                else "the nesting depth of a user-supplied container"
            )
            + "."
            if element_op
            else ""
        )
        return {
            "type": "missing_recursion_guard",
            "function": func["name"],
            "slot": slot or "not_a_slot",
            "shape": "self_recursion",
            "element_op": element_op or func["name"],
            "descent_via": element_op,
            "line": line,
            "confidence": "high" if (slot is not None or element_op) else "medium",
            "detail": (
                f"'{func['name']}' recurses into itself with no "
                "Py_EnterRecursiveCall guard — a deeply-nested or cyclic object "
                "overflows the C stack (SIGSEGV) instead of raising "
                f"RecursionError.{via} A guarded dispatcher does not help: it "
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
                _first_argument(c["arguments_text"]),
                params,
                roots,
                assignments,
                0,
                ctor_helpers,
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
                    "dispatcher": c["function_name"],
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
        dispatcher = primary["dispatcher"]
        dispatch_note = (
            "PyObject_Hash (Objects/object.c:1158)"
            if dispatcher == "PyObject_Hash"
            else (
                f"{dispatcher} (Include/internal/pycore_object.h:840 — a "
                "Py_ALWAYS_INLINE wrapper whose tail is PyObject_Hash)"
            )
        )

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
            why = f"'{func['name']}' calls {dispatcher} on " + (
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
                f"'{func['name']}' calls {dispatcher} on a parameter and is "
                f"called from recursion-prone slot(s) "
                f"{', '.join(sorted(slot_callers))} in the same file, so the "
                "descent is additive per nesting level"
            )
        else:
            shape = "hash_entry_point"
            confidence = "low"
            why = (
                f"'{func['name']}' calls {dispatcher} on a caller-supplied "
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
            "element_op": dispatcher,
            "argument_kind": kind,
            "tail_call": tail_call,
            "line": line,
            "sites": [
                {k: v for k, v in s.items() if k != "node"}
                for s in sorted(sites, key=lambda s: s["line"])
            ],
            "confidence": confidence,
            "detail": (
                f"{why}. {dispatch_note} is an "
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


# ---------------------------------------------------------------------------
# Shape 4: mutual recursion (issue #28 rule 3)
# ---------------------------------------------------------------------------
#
# ``update_subclasses:12359 <-> recurse_down_subclasses:12397`` is a
# two-function cycle that per-function analysis structurally cannot see: neither
# function calls itself.  It is an ASan-confirmed native stack overflow
# (CPY-0087).  The cycle is found from the same intra-file call graph
# ``scan_stw_safety`` builds.


def _strongly_connected(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan SCCs, iterative.  Only components of size >= 2 are returned.

    A size-1 component with a self-edge is ordinary self-recursion and is
    already shape 1, so it is excluded here rather than reported twice.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    out: list[list[str]] = []

    for root in graph:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child_i = work[-1]
            if child_i == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            recursed = False
            children = sorted(graph.get(node, ()))
            for i in range(child_i, len(children)):
                nxt = children[i]
                if nxt not in graph:
                    continue
                if nxt not in index:
                    work[-1] = (node, i + 1)
                    work.append((nxt, 0))
                    recursed = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if recursed:
                continue
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    component.append(w)
                    if w == node:
                        break
                if len(component) > 1:
                    out.append(component)
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return out


# Measured on Objects/ + Modules/ + Python/: every cycle in the tree is size 2
# or 3 except exactly one -- a 14-member component in Objects/typeobject.c that
# is the PyType_Ready / MRO machinery's normal connectivity, where each trip is
# bounded by MRO length rather than by user nesting. An SCC that large is not a
# recursion a reviewer can check; it is the call graph. Larger components are
# **counted in the envelope** (``large_cycles_not_reported``) rather than
# dropped silently, so the denominator stays visible.
_MAX_REPORTED_CYCLE = 3


def find_mutual_recursion(
    functions: list[dict],
    calls_by_func: list[list[dict]],
    slots: list[str | None],
    source_bytes: bytes,
    tree,
    oversized: list[dict] | None = None,
    already_reported: set[str] | None = None,
) -> list[dict]:
    """Cycles of two or more same-file functions with no guard anywhere in them.

    Three gates:

    * **no guard in any member** -- including a hand-rolled depth bound. A
      guard anywhere in the cycle bounds every trip round it, so the whole
      component is discharged by one macro.
    * **size <= _MAX_REPORTED_CYCLE.**
    * **some edge carries an element-derived argument.** CPython files are full
      of mutually recursive helper clusters that recurse over *fixed* structure
      (a parser, a marshaller); without this the rule would report every one of
      them. In the motivating cycle the evidence sits on
      ``recurse_down_subclasses``'s edge (``subclass`` comes from
      ``type_from_ref(ref)``, and ``ref`` out of ``PyDict_Next``), not on the
      edge the finding is reported at -- so the gate is a property of the
      cycle, not of the reported site.
    """
    by_name: dict[str, int] = {}
    for i, func in enumerate(functions):
        by_name.setdefault(func["name"], i)

    graph: dict[str, set[str]] = {}
    for func, calls in zip(functions, calls_by_func):
        graph.setdefault(func["name"], set())
        for c in calls:
            callee = c["function_name"]
            if callee in by_name and callee != func["name"]:
                graph[func["name"]].add(callee)

    findings: list[dict] = []
    for component in _strongly_connected(graph):
        members = sorted(component, key=lambda n: functions[by_name[n]]["start_line"])
        if any(
            _has_guard(functions[by_name[n]]) or _has_depth_bound(functions[by_name[n]])
            for n in members
        ):
            continue
        if already_reported and set(members) & already_reported:
            # A member is already reported as plainly self-recursive. One guard
            # discharges both, and the simpler statement is the useful one --
            # `_Py_uop_sym_set_const` is both self-recursive and in a 3-cycle.
            continue
        if len(members) > _MAX_REPORTED_CYCLE:
            if oversized is not None:
                oversized.append(
                    {
                        "head": members[0],
                        "line": functions[by_name[members[0]]]["start_line"],
                        "size": len(members),
                        "members": members,
                    }
                )
            continue

        descent_op: str | None = None
        descent_site: tuple[str, str, int] | None = None
        member_set = set(members)
        for name in members:
            idx = by_name[name]
            func = functions[idx]
            calls = calls_by_func[idx]
            assignments: dict[str, list[str]] = defaultdict(list)
            for a in find_assignments_in_scope(func["body_node"], source_bytes):
                if _IDENT_RE.fullmatch(a["variable"]):
                    assignments[a["variable"]].append(a["value_text"])
            derived = element_derived_names(calls, assignments)
            for c in calls:
                if c["function_name"] not in member_set:
                    continue
                op = descent_element_op(c["arguments_text"], derived)
                if op is not None:
                    descent_op = op
                    descent_site = (name, c["function_name"], c["start_line"])
                    break
            if descent_op is not None:
                break
        if descent_op is None:
            continue

        # Report at the first member in file order, on its edge into the cycle,
        # so the finding lands where a reader starts reading.
        head = members[0]
        head_idx = by_name[head]
        line = next(
            (
                c["start_line"]
                for c in calls_by_func[head_idx]
                if c["function_name"] in member_set
            ),
            functions[head_idx]["start_line"],
        )
        if is_suppressed_by_comment(source_bytes, tree, line):
            continue
        cycle_text = " -> ".join(members + [members[0]])
        assert descent_site is not None
        findings.append(
            {
                "type": "missing_recursion_guard",
                "function": head,
                "slot": slots[head_idx] or "not_a_slot",
                "shape": "mutual_recursion",
                "element_op": descent_op,
                "descent_via": descent_op,
                "cycle": members,
                "cycle_size": len(members),
                "descent_edge": {
                    "caller": descent_site[0],
                    "callee": descent_site[1],
                    "line": descent_site[2],
                },
                "line": line,
                "confidence": "high",
                "confirmed_by": "CPY-0087 (ASan-confirmed native stack overflow)"
                if set(members) == {"update_subclasses", "recurse_down_subclasses"}
                else None,
                "detail": (
                    f"{cycle_text} is a {len(members)}-function recursion cycle "
                    "with no Py_EnterRecursiveCall guard in any member — no "
                    "function in it calls itself, so per-function analysis "
                    "cannot see the recursion at all. "
                    f"{descent_site[0]} passes {descent_site[1]} a value from "
                    f"{descent_op} at line {descent_site[2]}, so the depth is "
                    "supplied by a Python-controlled graph and a deep or cyclic "
                    "one overflows the C stack (SIGSEGV) rather than raising "
                    "RecursionError. One guard anywhere in the cycle bounds "
                    "every trip round it."
                ),
            }
        )
    return findings


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
    oversized_cycles: list[dict] = []

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
        ctor_helpers = ctor_returning_helpers(functions)
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
                ctor_helpers,
            )
            if f is not None:
                f["file"] = rel
                findings.append(f)

        # Pass 3: cycles. Cross-function by construction, so it cannot live in
        # the per-function pass.
        file_oversized: list[dict] = []
        self_recursive = {
            f["function"]
            for f in findings
            if f.get("shape") == "self_recursion" and f.get("file") == rel
        }
        for f in find_mutual_recursion(
            functions,
            calls_by_func,
            slots,
            source_bytes,
            tree,
            file_oversized,
            self_recursive,
        ):
            f["file"] = rel
            findings.append(f)
        for entry in file_oversized:
            entry["file"] = rel
            oversized_cycles.append(entry)

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
            "unguarded": {
                "PyObject_Hash": "Objects/object.c:1158 — NO guard",
                "_PyObject_HashDictKey": (
                    "Include/internal/pycore_object.h:840 — Py_ALWAYS_INLINE "
                    "alias, tail-calls PyObject_Hash, so equally unguarded"
                ),
            },
            "guarded": _DISPATCH_GUARD_SITE,
            "bound_zero_excluded": sorted(_BOUND_ZERO_HASH),
            "note": (
                "Verified against CPython main @ 3.16.0a0. Only the hash "
                "dispatchers lack _Py_EnterRecursiveCallTstate, so only hash "
                "descents are unguarded at every level. Self-recursion is "
                "unguarded regardless of dispatcher. bound_zero_excluded lists "
                "hash spellings deliberately kept out of the vocabulary "
                "because they never descend into the object graph (identity, "
                "raw pointer, flat byte range)."
            ),
        },
        large_cycles_not_reported=oversized_cycles,
        cycle_reporting_cap=_MAX_REPORTED_CYCLE,
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

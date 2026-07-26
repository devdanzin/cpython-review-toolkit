#!/usr/bin/env python3
"""Scan CPython C source for memory-management bugs beyond reference counting.

Four syntactic checks, each with a distinct finding ``type``. All default to
silence — a candidate surfaces only when a specific, high-signal shape matches.

1. ``alloc_size_overflow`` (bug class R5; cf. gh-3493, gh-1779)
   A ``PyMem_Malloc(n * size)`` / ``malloc(count * sizeof(T))`` /
   ``PyMem_Realloc(p, n * k)`` where a multiply operand derives from an
   **unbounded** Python-controlled value (a ``PyLong_As*`` result, a
   protocol-dispatched ``PyObject_Length`` / ``PySequence_Size``, or a
   ``PyArg_Parse*`` output) and there is **no** prior bounds guard (a
   ``PY_SSIZE_T_MAX / size`` division check, a ``< 0`` sign check,
   ``__builtin_mul_overflow`` …).

   The taint table is split. ``Py_SIZE`` / ``PyTuple_GET_SIZE`` /
   ``PyList_GET_SIZE`` / ``PyBytes_GET_SIZE`` / ``PyUnicode_GET_LENGTH`` and the
   other concrete-type accessors are **bounded-by-an-existing-allocation**:
   an n-element container already occupies n * elemsize live bytes, so
   ``n * sizeof(ptr)`` cannot wrap. Such a multiply is dismissed unless it has a
   second non-constant factor. ``PyMem_New`` / ``PyMem_Resize`` / ``*_Calloc``
   overflow-check internally and never reach this check at all.

2. ``varobject_nitems_unguarded``
   ``PyObject_GC_NewVar`` / ``PyObject_NewVar`` / ``PyObject_GC_Resize`` (and
   the ``_Py`` spellings) whose ``nitems`` argument is neither narrow-typed,
   bounded by an existing allocation, nor preceded by an overflow guard. The
   multiply lives inside ``_PyObject_VAR_SIZE(tp, nitems)`` — it never appears
   in source, so rule 1 is *structurally* incapable of seeing it. There are
   exactly 9 var-object allocation sites in all of ``Objects/``.

   Live instance: ``Objects/structseq.c:77 PyStructSequence_New`` takes its
   size from the type's own ``n_fields`` dict entry, and every type built by the
   public ``PyStructSequence_NewType`` is a mutable heap type, so::

       import os
       os.terminal_size.n_fields = 2**62
       os.terminal_size((7, 9))   # ASan: heap-buffer-overflow WRITE at :235

   Guarded twin: ``Objects/tupleobject.c:52 tuple_alloc``, in structseq's own
   base type, has exactly the ``n > MAX/elem`` division guard.

3. ``gc_untrack_without_track`` (bug class O6; cf. gh-152107 OOM-0006/0017)
   A constructor allocates with ``PyObject_GC_New*`` and, on an early error
   path, frees the object (``Py_DECREF`` / ``Py_XDECREF`` / ``Py_CLEAR``)
   *before* any ``PyObject_GC_Track`` on it. Reachable only when that type's
   ``tp_dealloc`` runs the unchecked macro ``_PyObject_GC_UNTRACK``; the public
   function ``PyObject_GC_UnTrack`` re-checks ``_PyObject_GC_IS_TRACKED`` and is
   untracked-tolerant. (Untracked-tolerant, **not** NULL-safe —
   ``_PyObject_GC_IS_TRACKED`` dereferences its argument unconditionally.)

   The gate is *type-level*: resolve ``PyObject_GC_New(T, &SomeType)`` ->
   ``SomeType``'s ``tp_dealloc`` -> test that function's body, falling back to
   the file-level test only when the type object cannot be resolved in-file.

4. ``mismatched_alloc_free`` (allocator-family mismatch)
   The SAME variable is allocated by one allocator family and freed by another
   in the same function (``PyMem_Malloc`` freed with ``free`` / ``PyObject_Free``,
   ``PyObject_Malloc`` freed with ``PyMem_Free``, ``malloc`` freed with
   ``PyMem_Free`` …). The three families (raw ``malloc``, ``PyMem_*``,
   ``PyObject_*``) draw from different heaps and must not be crossed.

Out of scope: abort-vs-``MemoryError`` (triage class J). The bug this scanner
hunts is a *wrong-size* allocation followed by a write, not a failed one.

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

# Calls whose result is a Python-controlled integer, split by whether the value
# is *bounded by an existing allocation*.
#
# Bounded: a concrete-type size accessor. An n-element tuple/list/bytes object
# already occupies at least n * elemsize bytes of live memory, so n itself
# cannot exceed PY_SSIZE_T_MAX / elemsize — multiplying it by a small element
# size can never wrap. (`bounded-by-an-existing-allocation`, the single FP class
# behind 100% of this rule's noise on Objects/.)
_BOUNDED_LENGTH_CALLS = frozenset(
    {
        "Py_SIZE",
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
    }
)

# Unbounded: a raw integer conversion, a protocol-dispatched length (a Python
# __len__ / __length_hint__ may return any Py_ssize_t with no memory behind
# it), or a value read out of a mutable type dictionary.
_UNBOUNDED_INT_CALLS = frozenset(
    {
        "PyLong_AsSsize_t",
        "PyLong_AsLong",
        "PyLong_AsLongLong",
        "PyLong_AsSize_t",
        "PyLong_AsUnsignedLong",
        "PyLong_AsUnsignedLongLong",
        "PyLong_AsUnsignedLongMask",
        "PyNumber_AsSsize_t",
        "PyNumber_AsOff_t",
        "PyObject_Length",
        "PyObject_Size",
        "PyObject_LengthHint",
        "PySequence_Length",
        "PySequence_Size",
        "PyMapping_Length",
        "PyMapping_Size",
    }
)

_TAINT_CALL_NAMES = _BOUNDED_LENGTH_CALLS | _UNBOUNDED_INT_CALLS

# A non-tainted multiply operand no larger than this is treated as an element
# size (the `sizeof(T)` role), not as a second attacker-controlled factor.
_SMALL_FACTOR_MAX = 64

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


def _collect_taint(
    func: dict, source_bytes: bytes
) -> tuple[set[str], set[str], set[str]]:
    """Return (strong, weak, bounded) tainted variable names for a function.

    Strong: assigned from an *unbounded* Python-controlled integer call.
    Weak: written through ``&var`` by a PyArg_Parse* call (broad; lower signal).
    Bounded: assigned from a concrete-type size accessor — kept separate so the
    ``bounded-by-an-existing-allocation`` class can be dismissed.
    """
    strong: set[str] = set()
    weak: set[str] = set()
    bounded: set[str] = set()

    for assign in find_assignments_in_scope(func["body_node"], source_bytes):
        var = assign["variable"]
        if not var.isidentifier():
            continue
        head = _head_call(assign["value_text"])
        if head in _UNBOUNDED_INT_CALLS:
            strong.add(var)
        elif head in _BOUNDED_LENGTH_CALLS:
            bounded.add(var)
        elif head is None:
            # An arithmetic expression, e.g. `PyBytes_GET_SIZE(x) / sizeof(T)`.
            # Classify by which taint family the expression mentions.
            text = assign["value_text"]
            names = set(_HEAD_CALL_RE.findall(text))
            if names & _UNBOUNDED_INT_CALLS:
                strong.add(var)
            elif names & _BOUNDED_LENGTH_CALLS:
                bounded.add(var)

    for call in find_calls_in_scope(func["body_node"], source_bytes, _PYARG_PARSERS):
        for m in _ADDR_IDENT_RE.finditer(call["arguments_text"]):
            weak.add(m.group(1))

    return strong, weak, bounded


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


def _small_constant(node, source_bytes: bytes) -> bool:
    """True if ``node`` is a literal small enough to be an element size."""
    if node.type != "number_literal":
        return False
    text = get_node_text(node, source_bytes).rstrip("uUlL")
    try:
        return abs(int(text, 0)) <= _SMALL_FACTOR_MAX
    except ValueError:
        return False


def _arith_atoms(node):
    """Yield ``(kind, node)`` leaf operands of an arithmetic expression."""
    kind = node.type
    if kind == "sizeof_expression":
        yield ("sizeof", node)
        return
    if kind == "binary_expression":
        for field in ("left", "right"):
            child = node.child_by_field_name(field)
            if child is not None:
                yield from _arith_atoms(child)
        return
    if kind == "parenthesized_expression":
        for child in node.named_children:
            yield from _arith_atoms(child)
        return
    if kind in ("cast_expression", "unary_expression"):
        child = node.child_by_field_name("value") or node.child_by_field_name(
            "argument"
        )
        if child is not None:
            yield from _arith_atoms(child)
            return
    if kind == "call_expression":
        yield ("call", node)
        return
    if kind in ("identifier", "number_literal"):
        yield (kind, node)
        return
    yield ("other", node)


def _bounded_by_existing_allocation(bx, source_bytes: bytes, bounded: set[str]) -> bool:
    """True if the multiply is `<container length> * <element size>`.

    Exactly one operand derives from a concrete-type size accessor and every
    other leaf is a ``sizeof(...)`` or a small integer literal. Two independent
    length operands can still wrap, so those are *not* dismissed.
    """
    bounded_leaves = 0
    for kind, node in _arith_atoms(bx):
        if kind == "sizeof":
            continue
        if kind == "number_literal":
            if _small_constant(node, source_bytes):
                continue
            return False
        if kind == "identifier":
            if get_node_text(node, source_bytes) in bounded:
                bounded_leaves += 1
                continue
            return False
        if kind == "call":
            fn = node.child_by_field_name("function")
            name = get_node_text(fn, source_bytes) if fn is not None else ""
            if name in _BOUNDED_LENGTH_CALLS:
                bounded_leaves += 1
                continue
            return False
        return False
    return bounded_leaves == 1


def _check_alloc_size_overflow(func: dict, source_bytes: bytes, tree) -> list[dict]:
    strong, weak, bounded = _collect_taint(func, source_bytes)
    if not strong and not weak and not bounded:
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
            strong_hit = idents & (strong | _UNBOUNDED_INT_CALLS)
            weak_hit = idents & weak
            bounded_hit = idents & (bounded | _BOUNDED_LENGTH_CALLS)
            if not strong_hit and not weak_hit and not bounded_hit:
                continue

            # `<container length> * sizeof(T)` cannot wrap — the container is
            # already in memory, which is the bound.
            if (
                not strong_hit
                and not weak_hit
                and _bounded_by_existing_allocation(bx, source_bytes, bounded)
            ):
                continue

            guard_vars = ((strong_hit | bounded_hit) - _TAINT_CALL_NAMES) | weak_hit
            offset = call["start_byte"] - body_start
            pre_text = body[:offset] if 0 <= offset <= len(body) else body
            if _has_overflow_guard(pre_text, guard_vars):
                continue

            line = call["start_line"]
            if is_suppressed_by_comment(source_bytes, tree, line):
                continue

            confidence = "medium" if strong_hit else "low"
            tainted = idents & (strong | weak | bounded | _TAINT_CALL_NAMES)
            operand = ", ".join(sorted(tainted))
            source = (
                "unbounded (PyLong_As* / protocol length / parsed argument)"
                if strong_hit
                else "bounded-by-an-existing-allocation, but multiplied by "
                "another non-constant factor"
                if bounded_hit and not weak_hit
                else "a PyArg_Parse* output"
            )
            findings.append(
                {
                    "type": "alloc_size_overflow",
                    "function": func["name"],
                    "line": line,
                    "confidence": confidence,
                    "operands": sorted(tainted),
                    "detail": (
                        f"{call['function_name']}() size argument multiplies a "
                        f"Python-controlled operand ({operand}) — {source} — "
                        f"with no visible overflow guard (no PY_SSIZE_T_MAX/size "
                        f"division check, < 0 sign check, or "
                        f"__builtin_mul_overflow before the call). The product "
                        f"can wrap, under-allocating the buffer (bug class R5; "
                        f"cf. gh-3493, gh-1779). Use PyMem_New/PyMem_Calloc "
                        f"(they overflow-check) or add an explicit "
                        f"`n > PY_SSIZE_T_MAX / size` guard."
                    ),
                }
            )
    return findings


# ---------------------------------------------------------------------------
# Check 1b: unguarded nitems on a variable-length object allocation
# ---------------------------------------------------------------------------

# `PyObject_GC_NewVar(TYPE, typeobj, nitems)` and friends multiply nitems by
# tp_itemsize *inside* the `_PyObject_VAR_SIZE` macro, so the product never
# appears in source and `alloc_size_overflow` is structurally unable to see it.
# {allocator: index of the nitems argument}
_VAROBJ_NITEMS_INDEX = {
    "PyObject_NewVar": 2,
    "PyObject_GC_NewVar": 2,
    "PyObject_GC_Resize": 2,
    "_PyObject_NewVar": 1,
    "_PyObject_GC_NewVar": 1,
    "_PyObject_GC_Resize": 1,
    # The tp_alloc slot's default implementation. Reaches
    # _PyObject_VAR_SIZE(type, nitems) exactly as the *_NewVar family does.
    "PyType_GenericAlloc": 1,
    "_PyType_AllocNoTrack": 1,
}

# The same allocation written as a **virtual dispatch** through the slot
# pointer: `type->tp_alloc(type, n)`.  Keying on the callee's exact name misses
# every one of these, which is why the scanner reported zero on
# Objects/typeobject.c -- whose var-object sites both go through the slot.
# Tree-wide over Objects/ + Modules/ + Python/ that is **132 indirect sites**,
# only 15 of them with a non-constant `nitems`, plus 18 direct
# PyType_GenericAlloc calls. The 117 with a literal `0` are already discharged
# by the constant-count gate below, so modelling the slot costs almost nothing
# and is the difference between seeing the file's allocations and not.
_TP_ALLOC_SLOT_RE = re.compile(r"(?:->|\.)\s*tp_alloc\s*\)?\s*$")


def _varobj_nitems_index(callee: str) -> int | None:
    """Index of the ``nitems`` argument for a var-object allocation, or None.

    Handles the direct spellings and the virtual dispatch through the slot
    pointer, including the ``(*type->tp_alloc)(...)`` form.
    """
    name = callee.strip()
    if name in _VAROBJ_NITEMS_INDEX:
        return _VAROBJ_NITEMS_INDEX[name]
    if _TP_ALLOC_SLOT_RE.search(name):
        return 1
    return None

# An operand of one of these declared types cannot make `nitems * tp_itemsize`
# wrap a 64-bit size_t (LP64: int is 32-bit, tp_itemsize is a small constant).
_NARROW_INT_TYPES = frozenset(
    {
        "int",
        "unsigned",
        "unsigned int",
        "signed int",
        "short",
        "short int",
        "unsigned short",
        "char",
        "signed char",
        "unsigned char",
        "int8_t",
        "int16_t",
        "int32_t",
        "uint8_t",
        "uint16_t",
        "uint32_t",
        "_Bool",
        "bool",
    }
)

_TYPE_NOISE_RE = re.compile(r"\b(?:const|volatile|static|register|restrict)\b")


def _declared_types(func: dict, source_bytes: bytes) -> dict[str, str]:
    """Map local/parameter name -> normalized declared type text."""
    types: dict[str, str] = {}

    for param in func["parameters"].split(","):
        param = _TYPE_NOISE_RE.sub("", param).strip()
        m = re.match(
            r"^([A-Za-z_][\w\s]*?)\s+\**\s*([A-Za-z_]\w*)\s*(?:\[\s*\])?$", param
        )
        if m and "*" not in param.split(m.group(2))[0]:
            types[m.group(2)] = " ".join(m.group(1).split())

    for decl in walk_descendants(func["body_node"], "declaration"):
        type_node = decl.child_by_field_name("type")
        if type_node is None:
            continue
        type_text = " ".join(
            _TYPE_NOISE_RE.sub("", get_node_text(type_node, source_bytes)).split()
        )
        for child in decl.named_children:
            if child.type == "init_declarator":
                child = child.child_by_field_name("declarator")
            if child is None or child.type != "identifier":
                continue
            types.setdefault(get_node_text(child, source_bytes), type_text)

    return types


def _has_varobject_guard(pre_text: str) -> bool:
    """True if an *overflow* guard precedes the allocation.

    Deliberately stricter than ``_has_overflow_guard``: a bare ``n < 0`` sign
    check is not an overflow guard. ``PyStructSequence_New`` has exactly such a
    check and still hands ``2**62`` straight to ``_PyObject_VAR_SIZE``.
    """
    if any(c in pre_text for c in _MAX_CONSTS):
        return True
    return any(h in pre_text for h in _OVERFLOW_HELPERS)


def _check_varobject_nitems(
    func: dict, source_bytes: bytes, tree, census: dict[str, int] | None = None
) -> list[dict]:
    body = func["body"]
    body_start = func["body_node"].start_byte + 1
    types = _declared_types(func, source_bytes)
    strong, _weak, bounded = _collect_taint(func, source_bytes)

    findings: list[dict] = []
    for call in find_calls_in_scope(func["body_node"], source_bytes):
        idx = _varobj_nitems_index(call["function_name"])
        if idx is None:
            continue
        if census is not None:
            census["sites"] += 1
            if _TP_ALLOC_SLOT_RE.search(call["function_name"].strip()):
                census["via_slot_pointer"] += 1
        args_node = call["node"].child_by_field_name("arguments")
        if args_node is None:
            continue
        arg_nodes = [c for c in args_node.named_children if c.type != "comment"]
        if idx >= len(arg_nodes):
            continue
        nitems = arg_nodes[idx]
        nitems_text = " ".join(get_node_text(nitems, source_bytes).split())

        atoms = list(_arith_atoms(nitems))
        idents = [
            get_node_text(n, source_bytes) for kind, n in atoms if kind == "identifier"
        ]

        # A literal count, or one built only from small constants, is fine.
        if not idents and all(
            kind != "call"
            and (kind != "number_literal" or _small_constant(n, source_bytes))
            for kind, n in atoms
        ):
            continue
        if census is not None:
            census["non_constant_nitems"] += 1
        # Narrow declared types cannot overflow a 64-bit size_t.
        if idents and all(types.get(i, "") in _NARROW_INT_TYPES for i in idents):
            continue
        # Bounded by an object that is already in memory.
        if idents and all(i in bounded for i in idents):
            continue
        # Explicit overflow guard before the allocation.
        offset = call["start_byte"] - body_start
        pre_text = body[:offset] if 0 <= offset <= len(body) else body
        if _has_varobject_guard(pre_text):
            continue

        line = call["start_line"]
        if is_suppressed_by_comment(source_bytes, tree, line):
            continue

        confidence = "medium" if any(i in strong for i in idents) else "low"
        findings.append(
            {
                "type": "varobject_nitems_unguarded",
                "function": func["name"],
                "line": line,
                "confidence": confidence,
                "nitems": nitems_text,
                "operands": sorted(set(idents)),
                "dispatch": (
                    "slot_pointer"
                    if _TP_ALLOC_SLOT_RE.search(call["function_name"].strip())
                    else "direct"
                ),
                "detail": (
                    f"{call['function_name']}(..., {nitems_text}) reaches "
                    f"_PyObject_VAR_SIZE(tp, nitems) = "
                    f"tp_basicsize + nitems * tp_itemsize with **no** overflow "
                    f"guard before the call (no PY_SSIZE_T_MAX/itemsize "
                    f"division check, no __builtin_mul_overflow). The multiply "
                    f"is inside the macro, so it never appears in source and "
                    f"alloc_size_overflow cannot see it. If nitems is "
                    f"attacker-controlled the product wraps, the allocation "
                    f"succeeds far too small, and the first ob_item write is a "
                    f"heap overflow (Objects/structseq.c:77 "
                    f"PyStructSequence_New — reachable from pure Python via "
                    f"`os.terminal_size.n_fields = 2**62`). Guarded twin: "
                    f"Objects/tupleobject.c:52 tuple_alloc "
                    f"(`n > (PY_SSIZE_T_MAX - base) / sizeof(PyObject *)`)."
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
# tracked. The public *function* PyObject_GC_UnTrack is untracked-*tolerant*
# (it re-checks _PyObject_GC_IS_TRACKED — note: tolerant of an untracked
# object, NOT NULL-safe; _PyObject_GC_IS_TRACKED dereferences its argument
# unconditionally), so the ubiquitous "Py_DECREF(op) on error before GC_Track"
# idiom is correct wherever the destructor uses the function form.
#
# The gate is *type-level*: resolve PyObject_GC_New(T, &SomeType) -> SomeType's
# tp_dealloc -> test that function's body. A file-level test both misses
# cross-file deallocs (unsound) and lets a sibling type's macro re-admit a safe
# constructor (it produced the PyList_New false positive, where
# listiter_dealloc uses the macro but list_dealloc uses the function).
_GC_UNTRACK_MACRO = b"_PyObject_GC_UNTRACK"

_TYPE_OBJECT_DEF_RE = re.compile(
    rb"(?m)^[ \t]*(?:static\s+|extern\s+)*PyTypeObject\s+([A-Za-z_]\w*)\s*=\s*\{"
)
_TP_DEALLOC_DESIGNATED_RE = re.compile(
    r"\.tp_dealloc\s*=\s*(?:\(\s*destructor\s*\)\s*)?&?\s*([A-Za-z_]\w*)"
)
_TP_DEALLOC_POSITIONAL_RE = re.compile(r"([A-Za-z_]\w*)\s*,\s*/\*\s*tp_dealloc\s*\*/")


def _match_braces(source_bytes: bytes, open_index: int) -> int:
    """Return the index just past the ``}`` matching the ``{`` at open_index."""
    depth = 0
    i = open_index
    end = len(source_bytes)
    while i < end:
        ch = source_bytes[i : i + 1]
        if ch == b"{":
            depth += 1
        elif ch == b"}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return end


def _type_dealloc_map(source_bytes: bytes, functions: list[dict]) -> dict[str, str]:
    """Map static ``PyTypeObject`` symbol -> its ``tp_dealloc`` function name."""
    bodies = {f["name"]: f["body"] for f in functions}
    out: dict[str, str] = {}
    for m in _TYPE_OBJECT_DEF_RE.finditer(source_bytes):
        open_index = source_bytes.index(b"{", m.end() - 1)
        init_text = source_bytes[open_index : _match_braces(source_bytes, open_index)]
        text = init_text.decode("utf-8", "replace")
        dm = _TP_DEALLOC_DESIGNATED_RE.search(text) or _TP_DEALLOC_POSITIONAL_RE.search(
            text
        )
        if dm is None:
            continue
        dealloc = dm.group(1)
        if dealloc in bodies:
            out[m.group(1).decode("ascii", "replace")] = dealloc
    return out


_TYPE_ARG_RE = re.compile(r"^\s*&?\s*([A-Za-z_]\w*)\s*$")


def _resolve_untrack_variant(
    call_args: str, type_dealloc: dict[str, str], functions_by_name: dict[str, dict]
) -> tuple[str, str | None]:
    """Return (gate, dealloc_name) for a ``PyObject_GC_New*`` argument list.

    gate is ``"type:macro"`` (the O6 shape is live), ``"type:function"`` (safe),
    or ``"file"`` (unresolvable — the caller falls back to the file-level test).
    """
    parts = [p.strip() for p in call_args.split(",")]
    if len(parts) < 2:
        return "file", None
    m = _TYPE_ARG_RE.match(parts[1])
    if m is None:
        return "file", None
    dealloc = type_dealloc.get(m.group(1))
    if dealloc is None:
        return "file", None
    body = functions_by_name[dealloc]["body"]
    if _GC_UNTRACK_MACRO.decode() in body:
        return "type:macro", dealloc
    return "type:function", dealloc


def _check_gc_untrack_without_track(
    func: dict, source_bytes: bytes, tree, ctx: dict
) -> list[dict]:
    file_has_macro = _GC_UNTRACK_MACRO in source_bytes
    type_dealloc = ctx["type_dealloc"]
    functions_by_name = ctx["functions_by_name"]

    body_node = func["body_node"]

    # Objects allocated with a GC allocator (var -> allocator, first assign).
    gc_objs: dict[str, str] = {}
    gc_args: dict[str, str] = {}
    for assign in find_assignments_in_scope(body_node, source_bytes):
        var = assign["variable"]
        if not var.isidentifier() or var in gc_objs:
            continue
        head = _head_call(assign["value_text"])
        if head in _GC_ALLOCATORS:
            gc_objs[var] = head
            args = assign["value_text"]
            open_paren = args.find("(")
            gc_args[var] = (
                args[open_paren + 1 : args.rfind(")")] if open_paren >= 0 else ""
            )
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

        gate, dealloc = _resolve_untrack_variant(
            gc_args.get(var, ""), type_dealloc, functions_by_name
        )
        if gate == "type:function":
            # Resolved: this type's own tp_dealloc uses the untracked-tolerant
            # function PyObject_GC_UnTrack. The shape is benign here.
            continue
        if gate == "file" and not file_has_macro:
            # Unresolvable type (heap type from module state, itertype
            # parameter, …) and the file never uses the untrack macro at all.
            continue

        line = free["line"]
        if is_suppressed_by_comment(source_bytes, tree, line):
            continue
        if gate == "type:macro":
            evidence = (
                f"its tp_dealloc ({dealloc}) runs the unchecked macro "
                f"_PyObject_GC_UNTRACK"
            )
        else:
            evidence = (
                "the type could not be resolved in this file, which does use "
                "_PyObject_GC_UNTRACK somewhere (file-level fallback — confirm "
                "against this type's own tp_dealloc)"
            )
        findings.append(
            {
                "type": "gc_untrack_without_track",
                "function": func["name"],
                "line": line,
                "confidence": "medium" if gate == "type:macro" else "low",
                "gate": gate,
                "tp_dealloc": dealloc,
                "detail": (
                    f"'{var}' is allocated via {allocator}() and freed with "
                    f"{free['func']}() on an error path before any "
                    f"PyObject_GC_Track({var}), and {evidence}. The macro "
                    f"unconditionally unlinks an object it assumes is tracked, "
                    f"so a never-tracked object corrupts the GC list (debug "
                    f"build: _PyObject_AssertFailed, 'object not tracked by the "
                    f"garbage collector'). Bug class O6; cf. gh-152107 "
                    f"OOM-0006/0017. Ensure the error path does not reach an "
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


def _check_function(func: dict, source_bytes: bytes, tree, ctx: dict) -> list[dict]:
    findings: list[dict] = []
    findings.extend(_check_alloc_size_overflow(func, source_bytes, tree))
    findings.extend(
        _check_varobject_nitems(func, source_bytes, tree, ctx.get("varobj_census"))
    )
    findings.extend(_check_gc_untrack_without_track(func, source_bytes, tree, ctx))
    findings.extend(_check_mismatched_alloc_free(func, source_bytes, tree))
    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan for allocation-size overflow, GC-track, and allocator-mismatch bugs."""
    project_root, scan_root = resolve_roots(target)

    findings: list[dict] = []
    total_functions = 0
    files_analyzed = 0
    skipped: list[dict] = []
    # Denominator for the var-object rule: how many allocation sites the rule
    # even saw, how many of those dispatch through the tp_alloc slot pointer,
    # and how many survive the constant-count gate. A zero finding count with a
    # zero site count is silence, not safety.
    varobj_census = {"sites": 0, "via_slot_pointer": 0, "non_constant_nitems": 0}

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
            # A file the parser yielded nothing for vanishes from BOTH the
            # numerator and the denominator unless it is recorded. tree-sitter-c
            # cannot model a brace straddling #ifdef/#else, so a single such
            # construct can swallow most of a file (Modules/_io/fileio.c yields
            # 6 of 32 functions) or all of it -- and a silent drop then reads as
            # "clean" when it means "never looked".
            #
            # Only .c files are recorded: a header holding nothing but
            # declarations is the normal case and would bury the signal (104 of
            # them in Modules/ alone), whereas a .c file with zero extracted
            # functions is always worth a look.
            if filepath.suffix == ".c":
                skipped.append(
                    {"file": str(filepath), "reason": "no functions extracted"}
                )
            continue

        files_analyzed += 1
        rel = relpath(filepath, project_root)
        ctx = {
            "functions_by_name": {f["name"]: f for f in functions},
            "type_dealloc": _type_dealloc_map(source_bytes, functions),
            "varobj_census": varobj_census,
        }

        for func in functions:
            total_functions += 1
            for f in _check_function(func, source_bytes, tree, ctx):
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
        varobject_allocation_census=varobj_census,
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

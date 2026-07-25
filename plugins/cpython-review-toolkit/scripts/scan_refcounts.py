#!/usr/bin/env python3
"""Scan CPython C source for reference-counting errors.

The headline rule is **borrowed-ref-across-call**: a pointer that the code does
not own -- read out of a struct slot, out of a container with a borrowing
accessor, or aliased to a variable someone else is about to release -- is used
*after* an intervening call that can run arbitrary Python, with no
``Py_INCREF`` in between.  Re-entrancy (or a ``__del__``) invalidates the
pointer between the read and the use.

Rules, all reported with exact ``file:line`` coordinates:

``stale_slot_decref``
    ``local = obj->fld;`` ... a Python-reaching call ... ``obj->fld = NULL;``
    ``Py_DECREF(local);``.  Re-entrant code can have already cleared the slot
    and dropped the reference, so the second drop is a double-DECREF.  The fix
    is always ``Py_CLEAR(obj->fld)``, which re-reads the field.
    True positive: ``Objects/iterobject.c`` ``iter_iternext``.
    Guarded twin: ``calliter_iternext`` in the same file, which uses
    ``Py_CLEAR``.

``owner_freed_before_use``
    ``Py_DECREF(V);`` followed, in the same basic block, by a dereferencing use
    of ``V`` or of any variable aliased to it through ``A = B = expr``.
    True positive: ``Objects/genericaliasobject.c`` ``_Py_subs_parameters``,
    where ``args = tuple_args = PySequence_Tuple(args)`` aliases the two and
    the error branch reads ``args`` after ``Py_XDECREF(tuple_args)``.

``borrowed_ref_across_call``
    A borrowed pointer is *released* (``Py_DECREF`` / a stealing API) after a
    Python-reaching call, with no ``Py_INCREF`` in between -- ownership is
    dropped through a pointer the function never owned and may no longer be
    valid.  Deliberately narrower than "any use after any call": that broader
    form is unresolvable without dataflow and floods on CPython's own code.

``slot_transfer_across_call``
    The *escape* hazard, the second of the three things that can happen to a
    borrowed pointer after a Python-reaching call.  ``local = obj->fld`` ...
    call ... ``obj->fld = <new>`` ... ``return local``: the "we hold one
    reference to the old value; we'll either return it or keep it in the slot"
    transfer idiom, performed across a window in which a re-entrant call can
    perform the *same* transfer, so two callers each believe they own the single
    reference.  True positive: ``Modules/itertoolsmodule.c`` ``count_nextlong``
    (ASan heap-use-after-free).  Suppressed twin: ``Objects/enumobject.c``
    ``increment_longindex_lock_held``, a structural clone that is safe because
    both ``PyNumber_Add`` operands are provably ``PyLong``.

``stale_slot_use``
    The *deref/call* hazard.  ``local = obj->fld`` ... call ...
    ``Py_CLEAR(obj->fld)`` reachable ... ``local`` dereferenced or **called**.
    Strictly worse than a double-DECREF: ``slot_tp_iternext`` reads
    ``Py_TYPE(self)`` out of the freed block.  True positives:
    ``Modules/itertoolsmodule.c`` ``batched_next`` and ``islice_next``, both
    ASan heap-use-after-free.  Requires two primitives the release-only model
    did not need: runtime type-slot dispatch as a Python-reaching call
    (``iternext = *Py_TYPE(x)->tp_iternext; ... iternext(x)``) and loop-carried
    exposure, since the borrowed local often appears exactly once textually.

``potential_leak`` / ``potential_leak_on_error`` / ``potential_double_free``
    New-reference balance checks.  Heavily gated: assignments through ``*p =``
    or ``x->m =`` transfer ownership and are not leaks, ``Py_SETREF``'s second
    argument is consumed, a variable handed to an unmodelled call has escaped,
    and a variable assigned after the last ``goto`` cannot leak on an error
    path.

``init_not_reinit_safe`` / ``new_missing_member_init``
    ``tp_init`` / ``tp_new`` safety.  Both require *real slot registration*
    (``.tp_new =``, ``{Py_tp_new, X}``, or the positional static
    ``PyTypeObject`` form ``X, /* tp_new */`` that ``Objects/`` overwhelmingly
    uses) -- a name ending in ``_new`` is not a ``tp_new``.

Outputs a JSON structure per the toolkit's shared envelope.

Usage:
    python scan_refcounts.py [path]

    path: directory, file, or omitted for current directory
"""

import json
import re
import sys
from collections.abc import Generator
from pathlib import Path

# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def find_cpython_root(start: Path) -> Path | None:
    """Walk up from ``start`` looking for a CPython source tree root."""
    current = start if start.is_dir() else start.parent
    for _ in range(20):
        if (current / "Include" / "Python.h").is_file() and (
            current / "Objects" / "object.c"
        ).is_file():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".tox",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        ".eggs",
    }
)


def discover_c_files(
    root: Path,
    *,
    max_files: int = 0,
) -> Generator[Path, None, None]:
    """Yield ``.c``/``.h`` files under ``root``."""
    count = 0
    if root.is_file():
        if root.suffix in (".c", ".h"):
            yield root
        return
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix not in (".c", ".h"):
            continue
        parts = set(p.relative_to(root).parts)
        if parts & _EXCLUDE_DIRS:
            continue
        yield p
        count += 1
        if max_files and count >= max_files:
            return


def strip_comments_and_strings(source: str) -> str:
    """Blank out comments and string literals, preserving every newline.

    Line numbers derived from the returned text must agree with the input, so
    a multi-line block comment is replaced by the *same number of newlines*
    rather than collapsed to a single space.  Collapsing them was the cause of
    the tree-wide line-number drift (TK-15).
    """
    source = re.sub(
        r"/\*.*?\*/",
        lambda m: " " + "\n" * m.group(0).count("\n"),
        source,
        flags=re.DOTALL,
    )
    source = re.sub(r"//[^\n]*", " ", source)
    source = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', source)
    source = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", source)
    return source


# ---------------------------------------------------------------------------
# API classification tables
# ---------------------------------------------------------------------------


def with_private_aliases(names: frozenset[str]) -> frozenset[str]:
    """Add CPython's ``_Py``-prefixed private twin for each public name.

    Inside CPython's own tree the private alias is frequently the *only*
    spelling used: ``_PyErr_Occurred`` has 226 call sites, ``_PyNumber_Index``
    219, ``_PyErr_Format`` 97.  A table that lists only the public name is
    blind to all of them, which matters most for the Python-reaching set --
    a borrowed reference held across ``_PyObject_CallNoArgs`` is exactly the
    bug the borrowed-ref rules exist to find.
    """
    return frozenset(names) | frozenset(
        "_" + name for name in names if name.startswith("Py")
    )


NEW_REF_APIS = with_private_aliases(
    frozenset(
        {
            "PyObject_Call",
            "PyObject_CallObject",
            "PyObject_CallFunction",
            "PyObject_CallMethod",
            "PyObject_CallNoArgs",
            "PyObject_CallOneArg",
            "PyObject_GetAttr",
            "PyObject_GetAttrString",
            "PyObject_GetItem",
            "PyObject_Str",
            "PyObject_Repr",
            "PyObject_ASCII",
            "PyObject_Bytes",
            "PyObject_RichCompare",
            "PyObject_Format",
            "PyObject_Vectorcall",
            "PyUnicode_FromString",
            "PyUnicode_FromFormat",
            "PyUnicode_Decode",
            "PyUnicode_FromEncodedObject",
            "PyUnicode_Join",
            "PyUnicode_FromObject",
            "PyUnicode_Substring",
            "PyBytes_FromString",
            "PyBytes_FromStringAndSize",
            "PyBytes_FromObject",
            "PyLong_FromLong",
            "PyLong_FromUnsignedLong",
            "PyLong_FromDouble",
            "PyLong_FromLongLong",
            "PyLong_FromSsize_t",
            "PyLong_FromSize_t",
            "PyFloat_FromDouble",
            "PyFloat_FromString",
            "PyList_New",
            "PyList_GetSlice",
            "PyTuple_New",
            "PyTuple_GetSlice",
            "PyTuple_Pack",
            "PyDict_New",
            "PyDict_Copy",
            "PyDict_Keys",
            "PyDict_Values",
            "PyDict_Items",
            "PySet_New",
            "PyFrozenSet_New",
            "Py_BuildValue",
            "Py_VaBuildValue",
            "PySequence_List",
            "PySequence_Tuple",
            "PySequence_GetItem",
            "PySequence_Concat",
            "PySequence_InPlaceConcat",
            "PyNumber_Add",
            "PyNumber_Subtract",
            "PyNumber_Multiply",
            "PyNumber_TrueDivide",
            "PyNumber_FloorDivide",
            "PyNumber_Remainder",
            "PyNumber_Power",
            "PyNumber_Negative",
            "PyNumber_Positive",
            "PyNumber_Absolute",
            "PyNumber_Long",
            "PyNumber_Float",
            "PyNumber_Index",
            "PyNumber_InPlaceAdd",
            "PyNumber_InPlaceSubtract",
            "PyIter_Next",
            "PyImport_ImportModule",
            "PyImport_Import",
            "PyModule_New",
            "PyModule_NewObject",
            "PyType_FromSpec",
            "PyType_FromSpecWithBases",
            "PyType_FromModuleAndSpec",
            "_PyObject_New",
            "PyObject_Init",
            "PyErr_NewException",
            "PyErr_NewExceptionWithDoc",
            "PyMapping_Keys",
            "PyMapping_Values",
            "PyMapping_Items",
            "PyObject_GenericGetAttr",
            "PyCapsule_New",
            "PyMemoryView_FromObject",
            "PyWeakref_NewRef",
            "PyWeakref_NewProxy",
            "PyStructSequence_New",
            "PyCode_New",
            "PyCode_NewEmpty",
            "PyFrame_New",
        }
    )
)

BORROWED_REF_APIS = with_private_aliases(
    frozenset(
        {
            "PyList_GetItem",
            "PyList_GET_ITEM",
            "PyTuple_GetItem",
            "PyTuple_GET_ITEM",
            "PyDict_GetItem",
            "PyDict_GetItemString",
            "PyDict_GetItemWithError",
            "PyModule_GetDict",
            "PyImport_GetModuleDict",
            "PyThreadState_GetDict",
            "PySys_GetObject",
            "PyWeakref_GetObject",
            "PyWeakref_GET_OBJECT",
            "PyErr_Occurred",
            "PyMethod_GET_SELF",
            "PyMethod_GET_FUNCTION",
            "PyCell_GET",
            "Py_None",
            "Py_True",
            "Py_False",
            "PyExc_TypeError",
            "PyExc_ValueError",
            "PyExc_KeyError",
            "PyExc_AttributeError",
            "PyExc_RuntimeError",
        }
    )
)

# The subset of BORROWED_REF_APIS that is actually *called* and hands back a
# borrowed item, plus the borrowing accessors the original table missed.
# ``Py_None`` and the ``PyExc_*`` singletons are immortal and are excluded on
# purpose -- they can never dangle.
BORROWED_GETTER_APIS = with_private_aliases(
    frozenset(
        {
            "PyList_GetItem",
            "PyList_GET_ITEM",
            "PyTuple_GetItem",
            "PyTuple_GET_ITEM",
            "PyDict_GetItem",
            "PyDict_GetItemString",
            "PyDict_GetItemWithError",
            "_PyDict_GetItemStringWithError",
            "PySequence_Fast_GET_ITEM",
            "PyStructSequence_GET_ITEM",
            "PyModule_GetDict",
            "PyImport_GetModuleDict",
            "PyThreadState_GetDict",
            "PySys_GetObject",
            "PySys_GetObjectId",
            "PyWeakref_GetObject",
            "PyWeakref_GET_OBJECT",
            "PyMethod_GET_SELF",
            "PyMethod_GET_FUNCTION",
            "PyFunction_GET_CODE",
            "PyFunction_GET_GLOBALS",
            "PyFunction_GET_DEFAULTS",
            "PyFunction_GET_CLOSURE",
            "PyCell_GET",
            "PyEval_GetBuiltins",
            "PyEval_GetGlobals",
            "PyEval_GetLocals",
        }
    )
)

STEAL_REF_APIS = with_private_aliases(
    frozenset(
        {
            "PyList_SET_ITEM",
            "PyList_SetItem",
            "PyTuple_SET_ITEM",
            "PyTuple_SetItem",
            "PyModule_AddObject",
        }
    )
)

INCREF_APIS = with_private_aliases(
    frozenset(
        {
            "Py_INCREF",
            "Py_XINCREF",
            "Py_NewRef",
            "Py_XNewRef",
            "_Py_NewRef",
            "_Py_XNewRef",
            "_Py_TryIncref",
        }
    )
)

DECREF_APIS = with_private_aliases(
    frozenset(
        {
            "Py_DECREF",
            "Py_XDECREF",
            "Py_CLEAR",
            "Py_SETREF",
            "Py_XSETREF",
        }
    )
)

# ---------------------------------------------------------------------------
# Calls that can execute arbitrary Python code
# ---------------------------------------------------------------------------
#
# This is the table the borrowed-ref rules are built on.  A borrowed pointer is
# only in danger if something between the read and the use can re-enter the
# interpreter.  ``Py_DECREF`` is in here deliberately: dropping the last
# reference runs ``tp_dealloc`` and therefore a Python-level ``__del__``, which
# is what makes the ``genericaliasobject.c`` owner-freed shape detectable.

PYTHON_REACHING_APIS = with_private_aliases(
    frozenset(
        {
            # Refcount drops: tp_dealloc / __del__ / weakref callbacks are Python.
            "Py_DECREF",
            "Py_XDECREF",
            "Py_CLEAR",
            "Py_SETREF",
            "Py_XSETREF",
            "_Py_Dealloc",
            # Direct invocation.
            "PyObject_CallObject",
            "PyObject_CallFunction",
            "PyObject_CallFunctionObjArgs",
            "PyObject_CallMethod",
            "PyObject_CallMethodObjArgs",
            "PyObject_CallMethodNoArgs",
            "PyObject_CallMethodOneArg",
            "PyObject_CallNoArgs",
            "PyObject_CallOneArg",
            "PyObject_CallFinalizer",
            "PyObject_CallFinalizerFromDealloc",
            "PyEval_CallObject",
            "PyEval_EvalCode",
            "PyEval_EvalCodeEx",
            "_PyEval_GetBuiltin",
            "_PyObject_MakeTpCall",
            # Attribute and item protocols.
            "PyObject_GetAttr",
            "PyObject_GetAttrString",
            "PyObject_SetAttr",
            "PyObject_SetAttrString",
            "PyObject_DelAttr",
            "PyObject_DelAttrString",
            "PyObject_HasAttr",
            "PyObject_HasAttrString",
            "PyObject_HasAttrWithError",
            "PyObject_HasAttrStringWithError",
            "PyObject_GetOptionalAttr",
            "PyObject_GetOptionalAttrString",
            "PyObject_GenericGetAttr",
            "PyObject_GenericSetAttr",
            "_PyObject_LookupSpecial",
            "_PyObject_LookupAttr",
            "PyObject_GetItem",
            "PyObject_SetItem",
            "PyObject_DelItem",
            "PyObject_GetOptionalItem",
            # Comparison, hashing, stringification, truthiness.
            "PyObject_Hash",
            "PyObject_RichCompare",
            "PyObject_RichCompareBool",
            "PyObject_Repr",
            "PyObject_Str",
            "PyObject_ASCII",
            "PyObject_Bytes",
            "PyObject_Format",
            "PyObject_Print",
            "PyObject_IsTrue",
            "PyObject_Not",
            "PyObject_Size",
            "PyObject_Length",
            "PyObject_IsInstance",
            "PyObject_IsSubclass",
            "_PyObject_RealIsInstance",
            "_PyObject_RealIsSubclass",
            # Iteration.
            "PyIter_Next",
            "PyIter_Send",
            "PyObject_GetIter",
            "PyObject_GetAIter",
            # Container operations that reach __hash__ / __eq__ / __lt__.
            "PyDict_GetItem",
            "PyDict_GetItemString",
            "PyDict_GetItemWithError",
            "PyDict_GetItemRef",
            "PyDict_SetItem",
            "PyDict_SetItemString",
            "PyDict_DelItem",
            "PyDict_DelItemString",
            "PyDict_Contains",
            "PyDict_SetDefault",
            "PyDict_SetDefaultRef",
            "PyDict_Pop",
            "PyDict_Update",
            "PyDict_Merge",
            "PyDict_MergeFromSeq2",
            "PyList_Append",
            "PyList_Insert",
            "PyList_Sort",
            "PyList_SetSlice",
            "PySet_Add",
            "PySet_Contains",
            "PySet_Discard",
            "PySet_Pop",
            # Warnings, auditing and %R/%S/%T-style error formatting.
            "PyErr_WarnEx",
            "PyErr_WarnFormat",
            "PyErr_WarnExplicit",
            "PyErr_ResourceWarning",
            "PyErr_Format",
            "PyErr_FormatV",
            "PyErr_SetObject",
            "PyErr_WriteUnraisable",
            "PyErr_Print",
            "PySys_Audit",
            "PySys_AuditTuple",
            "PyUnicode_FromFormat",
            "PyUnicode_FromFormatV",
            # Argument parsing runs "O&" converter callbacks.
            "PyArg_Parse",
            "PyArg_ParseTuple",
            "PyArg_ParseTupleAndKeywords",
            "PyArg_VaParse",
            "PyArg_VaParseTupleAndKeywords",
            "_PyArg_ParseTupleAndKeywordsFast",
            "_PyArg_ParseStackAndKeywords",
            "PyUnicode_FSConverter",
            "PyUnicode_FSDecoder",
            "PyObject_GetBuffer",
            # Allocation paths that can trigger a GC pass, which runs finalizers.
            "PyObject_GC_New",
            "PyObject_GC_NewVar",
            "PyObject_GC_Resize",
            "_PyObject_GC_New",
            "_PyObject_GC_NewVar",
            "_PyObject_GC_Malloc",
            "_PyObject_GC_Resize",
            "PyType_GenericAlloc",
            "PyGC_Collect",
            # Imports execute module bodies.
            "PyImport_Import",
            "PyImport_ImportModule",
            "PyImport_ImportModuleLevelObject",
        }
    )
)

# Whole families that reach Python through a type's slots.  Matched by prefix.
_PY_REACHING_FAMILY_RE = (
    r"_?(?:PySequence|PyMapping|PyNumber)_\w+"
    r"|_?PyObject_(?:Call|Vectorcall)\w*"
)

# ... except these, which are pure macros / type checks and run nothing.
_PY_REACHING_EXCLUDE = frozenset(
    {
        "PySequence_Fast_GET_ITEM",
        "PySequence_Fast_GET_SIZE",
        "PySequence_Fast_ITEMS",
        "PySequence_Check",
        "PyMapping_Check",
        "PyNumber_Check",
        "PyObject_CallableCheck",
    }
)

_PY_REACHING_RE = re.compile(
    r"\b("
    + "|".join(
        re.escape(api) for api in sorted(PYTHON_REACHING_APIS, key=len, reverse=True)
    )
    + r"|"
    + _PY_REACHING_FAMILY_RE
    + r")\s*\("
)

_REFCOUNT_ONLY_REACHING = frozenset(
    {
        "Py_DECREF",
        "Py_XDECREF",
        "Py_CLEAR",
        "Py_SETREF",
        "Py_XSETREF",
        "_Py_Dealloc",
    }
)


def python_reaching_calls(text: str, start: int, end: int) -> list[str]:
    """Return the names of Python-reaching calls in ``text[start:end]``."""
    names: list[str] = []
    for m in _PY_REACHING_RE.finditer(text, start, end):
        name = m.group(1)
        if name in _PY_REACHING_EXCLUDE:
            continue
        names.append(name)
    return names


# ---------------------------------------------------------------------------
# Python-reaching call, spelled as a runtime type-slot dispatch
# ---------------------------------------------------------------------------
#
# ``iternext = *Py_TYPE(it)->tp_iternext; ... iternext(it)`` is invisible to a
# name table, and it is how ``batched_next`` / ``islice_next`` reach arbitrary
# Python.  The inline form ``(*Py_TYPE(it)->tp_iternext)(it)`` appears four
# times in ``Modules/itertoolsmodule.c`` alone.
#
# A *statically named* type (``PyUnicode_Type.tp_hash``) is deliberately not
# matched: its slot is known at compile time and cannot be a Python callable.
# That is the same line the false-positive taxonomy already draws under
# "Statically-known type slot".

_RUNTIME_SLOTS = (
    "tp_iternext",
    "tp_iter",
    "tp_call",
    "tp_richcompare",
    "tp_hash",
    "tp_repr",
    "tp_str",
    "tp_getattro",
    "tp_setattro",
    "tp_descr_get",
    "tp_descr_set",
    "bf_getbuffer",
)
_RUNTIME_SLOT_ALT = "|".join(_RUNTIME_SLOTS)
_RUNTIME_TYPE_EXPR = r"(?:Py_TYPE\s*\([^)]*\)|[A-Za-z_]\w*\s*->\s*ob_type)"

# ``(*Py_TYPE(x)->tp_iternext)(x)`` / ``x->ob_type->tp_hash(x)``
_INLINE_SLOT_CALL_RE = re.compile(
    r"\(\s*\*?\s*"
    + _RUNTIME_TYPE_EXPR
    + r"\s*->\s*("
    + _RUNTIME_SLOT_ALT
    + r")\s*\)\s*\("
)
# ``f = *Py_TYPE(x)->tp_iternext;`` — the pointer is called later, possibly in
# a loop, possibly many lines away.
_SLOT_FPTR_ASSIGN_RE = re.compile(
    r"([A-Za-z_]\w*)\s*=\s*\*?\s*"
    + _RUNTIME_TYPE_EXPR
    + r"\s*->\s*("
    + _RUNTIME_SLOT_ALT
    + r")"
)


def reaching_calls_with_slots(clean: str, start: int, end: int) -> list[str]:
    """``python_reaching_calls`` plus runtime type-slot dispatch.

    ``clean`` must be the *whole* function body: a slot function pointer is
    frequently assigned above the region under test and called inside it.
    """
    names = [
        n
        for n in python_reaching_calls(clean, start, end)
        if n not in _REFCOUNT_ONLY_REACHING
    ]
    segment = clean[start:end]
    for m in _INLINE_SLOT_CALL_RE.finditer(segment):
        names.append(f"*Py_TYPE(...)->{m.group(1)}()")
    for m in _SLOT_FPTR_ASSIGN_RE.finditer(clean):
        fptr, slot = m.group(1), m.group(2)
        if re.search(rf"\b{re.escape(fptr)}\s*\(", segment):
            names.append(f"{fptr}() [= Py_TYPE(...)->{slot}]")
    return names


# ---------------------------------------------------------------------------
# Statement-shape regexes
# ---------------------------------------------------------------------------

# ``x = API(`` -- but not ``obj->x =``, ``s.x =`` or ``*p =``, all of which
# transfer ownership somewhere the scanner cannot follow.
_ASSIGN_CALL_RE = re.compile(
    r"(\w+)\s*=\s*(?:\(\s*[\w\s*]+\)\s*)?("
    + "|".join(re.escape(api) for api in sorted(NEW_REF_APIS, key=len, reverse=True))
    + r")\s*\("
)

# Ownership handed to a struct member or through a pointer out-parameter.
_TRANSFER_ASSIGN_RE = re.compile(
    r"(?:->|\.|\*)\s*(\w+)\s*=\s*(?:\(\s*[\w\s*]+\)\s*)?("
    + "|".join(re.escape(api) for api in sorted(NEW_REF_APIS, key=len, reverse=True))
    + r")\s*\("
)

# Permissive: used to build *suppression* sets, where over-matching is safe.
_DECREF_RE = re.compile(
    r"\b(Py_DECREF|Py_XDECREF|Py_CLEAR|Py_SETREF|Py_XSETREF)\s*\(\s*(\w+)"
)

# Strict: a bare identifier argument, used where the *identity* of the operand
# matters.  ``Py_CLEAR(self->field)`` must not read as "self was cleared".
_BARE_DECREF_RE = re.compile(
    r"\b(Py_DECREF|Py_XDECREF|Py_CLEAR)\s*\(\s*([A-Za-z_]\w*)\s*\)"
)

# ``Py_SETREF(dst, src)`` consumes *src* too.
_SETREF_RE = re.compile(r"\bPy_X?SETREF\s*\(\s*([\w>.\-]+)\s*,\s*(\w+)")

_INCREF_RE = re.compile(
    r"\b("
    + "|".join(sorted(INCREF_APIS, key=len, reverse=True))
    + r")\s*\(\s*([A-Za-z_]\w*)\s*\)"
)

# Capture the last argument (the stolen reference) before the closing paren.
_STEAL_CALL_RE = re.compile(
    r"\b("
    + "|".join(re.escape(api) for api in sorted(STEAL_REF_APIS, key=len, reverse=True))
    + r")\s*\([^)]*,\s*(\w+)\s*\)"
)

# The right-hand side of a plain assignment.
# Braces are allowed: CPython assigns compound literals
# (`value->_m_dict = (struct cached_m_dict){ .copied=copied };`).
_ASSIGN_RHS_RE = re.compile(r"(?<![=!<>+\-*/%&|^])=(?!=)([^;]*);")

_RETURN_RE = re.compile(r"\breturn\s+(\w+)\s*;")
_GOTO_ERROR_RE = re.compile(r"\bgoto\s+(\w+)\s*;")
# CPython indents cleanup labels by one or two spaces as often as not, so the
# label must be allowed leading whitespace -- but `case`/`default` inside a
# switch are not cleanup labels.
_ERROR_LABEL_RE = re.compile(
    r"^[ \t]*(?!case\b|default\b)([A-Za-z_]\w*)[ \t]*:(?!:)", re.MULTILINE
)

# ``local = obj->fld;`` with an optional declaration and/or cast.
_SLOT_LOAD_RE = re.compile(
    r"^[ \t]*(?:(?:const\s+)?[A-Za-z_]\w*\s*\*+\s*)?([A-Za-z_]\w*)\s*=\s*"
    r"(?:\(\s*[\w\s*]+\)\s*)?([A-Za-z_]\w*)\s*->\s*([A-Za-z_]\w*)\s*;",
    re.MULTILINE,
)

# ``local = BorrowingAccessor(...)``.
_BORROWED_CALL_LOAD_RE = re.compile(
    r"(?<![\w.>*])([A-Za-z_]\w*)\s*=\s*(?:\(\s*[\w\s*]+\)\s*)?("
    + "|".join(
        re.escape(api) for api in sorted(BORROWED_GETTER_APIS, key=len, reverse=True)
    )
    + r")\s*\("
)

# ``A = B = expr;`` makes A and B aliases of one another.
_CHAIN_ASSIGN_RE = re.compile(
    r"(?<![\w.>*=!<])([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*=\s*(?![=])"
)

_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(((?:[^()]|\([^()]*\))*)\)")

_IDENT_RE = re.compile(r"[A-Za-z_]\w*")

# An identifier that is not a struct member access.
_BARE_IDENT_RE = re.compile(r"(?<![\w.>])[A-Za-z_]\w*")

_BLOCK_TERMINATOR_RE = re.compile(r"\b(return|goto|break|continue)\b")

# Calls that cannot make their arguments escape: refcount macros, type checks
# and size/type accessors.
_NO_ESCAPE_CALL_RE = re.compile(
    r"^(?:Py_(?:X?INCREF|X?DECREF|CLEAR|X?SETREF|X?NewRef|TYPE|SIZE|REFCNT"
    r"|IS_TYPE|Is|IsNone|IsTrue|IsFalse)"
    r"|_Py_(?:X?NewRef|IsImmortal|TryIncref|Dealloc)"
    r"|assert|sizeof|memset|memcpy"
    r"|Py\w*_Check(?:Exact)?"
    r"|Py\w*_GET_SIZE|Py\w*_GET_ITEM)$"
)

# Calls that do *not* dereference their pointer argument, for the purpose of
# deciding whether a freed pointer was read again.  Distinct from
# _NO_ESCAPE_CALL_RE: ``PyTuple_GET_ITEM(t, i)`` does not take ownership of
# ``t`` but very much does dereference it.
_NON_DEREF_CALL_RE = re.compile(
    r"^(?:Py_(?:X?INCREF|X?DECREF|CLEAR|X?SETREF)|sizeof"
    r"|\w*assert\w*|_?Py\w*_ASSERT\w*)$",
    re.IGNORECASE,
)

# A preprocessor branch boundary: code after it is in a *different*
# translation, so a Py_DECREF before an `#else` and a use after it are
# mutually exclusive, not sequential.
_PREPROC_BRANCH_RE = re.compile(
    r"^[ \t]*#[ \t]*(?:if|ifdef|ifndef|elif|else|endif)\b", re.MULTILINE
)

# A `goto` target: code after it is reachable from elsewhere, so text order
# does not establish that it runs after the statement above it.
_GOTO_LABEL_RE = re.compile(r"^[ \t]*([A-Za-z_]\w*)[ \t]*:(?!:)[ \t]*$", re.MULTILINE)

# Calls that hand ownership to a container/module that then keeps the object
# alive.  A `Py_DECREF` after one of these is dropping the *caller's* extra
# reference, not the last one.
_PUBLISH_APIS = with_private_aliases(
    frozenset(
        {
            "PyModule_AddType",
            "PyModule_AddObject",
            "PyModule_AddObjectRef",
            "PyDict_SetItem",
            "PyDict_SetItemString",
            "PyList_Append",
            "PyList_SetItem",
            "PyList_SET_ITEM",
            "PyTuple_SetItem",
            "PyTuple_SET_ITEM",
            "PySet_Add",
            "PyObject_SetAttr",
            "PyObject_SetAttrString",
            "PyObject_SetItem",
            "PyStructSequence_SetItem",
            "PyType_Ready",
            "PyModule_AddIntConstant",
            "PyModule_Add",
        }
    )
)

_PUBLISH_CALL_RE = re.compile(
    r"\b("
    + "|".join(sorted(_PUBLISH_APIS, key=len, reverse=True))
    + r")\s*\(((?:[^()]|\([^()]*\))*)\)"
)


# ``foo(&var)`` re-binds ``var`` through an out-parameter.
def _outparam_rebind_re(name: str) -> re.Pattern:
    """Regex matching ``&name`` handed to a call as an out-parameter."""
    return re.compile(rf"&\s*{re.escape(name)}\b")


# ---------------------------------------------------------------------------
# Function detection
# ---------------------------------------------------------------------------

_SKIP_NAMES = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "do",
        "else",
        "sizeof",
        "return",
        "typedef",
        "struct",
        "union",
        "enum",
        "defined",
    }
)


def find_functions(source: str) -> list[dict]:
    """Find C function definitions and extract their bodies.

    Handles both single-line and multi-line signatures, including
    Argument Clinic ``_impl`` functions with ``/*[clinic ...]*/``
    comments between ``)`` and ``{``.

    Each entry carries ``body_start_line``: the 1-based source line of the
    first line of ``body``.  Every finding's absolute line is computed from
    that, so a finding's ``line`` is exact rather than approximated from the
    signature.
    """
    lines = source.split("\n")
    functions: list[dict] = []

    for i, line in enumerate(lines):
        if not line.startswith("{"):
            continue
        if i < 1:
            continue

        # Look backwards up to 10 lines to assemble the full signature.
        # Skip comment lines (e.g. Argument Clinic markers).
        sig_lines: list[str] = []
        sig_start = i - 1
        for k in range(i - 1, max(i - 11, -1), -1):
            stripped = lines[k].strip()
            # Skip blank lines and comment-only lines.
            if not stripped or stripped.startswith(("/*", "*")):
                continue
            sig_lines.insert(0, stripped)
            # Check if this line contains `name(` at column 0 or after
            # a return type — that's the start of the signature.
            if "(" in stripped:
                sig_start = k
                break

        if not sig_lines:
            continue

        # Join the signature lines and try to extract name(params).
        sig = " ".join(sig_lines)
        # Remove Argument Clinic end markers.
        sig = re.sub(r"/\*\[clinic.*?\]\*/", "", sig).strip()

        # Try to match: [return_type] name(params)
        m = re.match(r"(?:[\w\s\*]+?)\s+(\w+)\s*\(([^)]*)\)\s*$", sig)
        if not m:
            # Try: name(params) alone
            m = re.match(r"^(\w+)\s*\(([^)]*)\)\s*$", sig)
        if not m:
            continue

        func_name = m.group(1)
        if func_name in _SKIP_NAMES:
            continue

        # Find the matching closing brace.
        depth = 1
        body_start = i + 1
        body_end = body_start
        for j in range(body_start, len(lines)):
            for ch in lines[j]:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        body_end = j
                        break
            if depth == 0:
                break

        body = "\n".join(lines[body_start:body_end])

        # Detect return type line (may be 1-2 lines above sig_start).
        actual_start = sig_start
        if actual_start > 0 and re.match(
            r"^[\w\s\*]+$", lines[actual_start - 1].strip()
        ):
            actual_start -= 1

        functions.append(
            {
                "name": func_name,
                "params": m.group(2),
                "body": body,
                "start_line": actual_start + 1,
                "body_start_line": body_start + 1,
                "end_line": body_end + 1,
                "raw_lines": lines[body_start:body_end],
            }
        )
    return functions


# ---------------------------------------------------------------------------
# Type-slot registration
# ---------------------------------------------------------------------------
#
# A function is a ``tp_new`` only if some ``PyTypeObject`` / ``PyType_Slot``
# actually registers it as one.  ``Objects/`` overwhelmingly uses the
# *positional* static initialiser, where the only marker is a trailing comment
# — so this must run on the RAW source, before comments are stripped.

_DESIGNATED_SLOT_RE = re.compile(
    r"\.\s*(tp_\w+)\s*=\s*(?:\(\s*[\w\s*]+\)\s*)?([A-Za-z_]\w*)"
)

_SPEC_SLOT_RE = re.compile(
    r"\{\s*Py_(tp_\w+)\s*,\s*(?:\(\s*[\w\s*]+\)\s*)?\(?\s*([A-Za-z_]\w*)"
)

_POSITIONAL_SLOT_RE = re.compile(
    r"(?:\(\s*[\w\s*]+\)\s*)?([A-Za-z_]\w*)\s*,\s*/\*\s*(tp_\w+)\s*\*/"
)


def collect_slot_registrations(raw_source: str) -> dict[str, set[str]]:
    """Map ``tp_*`` slot name -> set of C functions registered in that slot.

    Recognises all three CPython spellings:

    * designated static initialiser -- ``.tp_new = foo_new,``
    * heap-type slot array -- ``{Py_tp_new, foo_new},``
    * positional static initialiser -- ``(newfunc)foo_new,  /* tp_new */``

    The last form is only visible in the *raw* source: the slot name lives in
    a comment.  It is also the dominant form in ``Objects/`` (42 positional
    versus 2 designated at 3.16.0a0).
    """
    slots: dict[str, set[str]] = {}

    def add(slot: str, name: str) -> None:
        if name in ("0", "NULL"):
            return
        slots.setdefault(slot, set()).add(name)

    for m in _DESIGNATED_SLOT_RE.finditer(raw_source):
        add(m.group(1), m.group(2))
    for m in _SPEC_SLOT_RE.finditer(raw_source):
        add(m.group(1), m.group(2))
    for m in _POSITIONAL_SLOT_RE.finditer(raw_source):
        add(m.group(2), m.group(1))
    return slots


def _registered_as(func: dict, slots: dict[str, set[str]], slot: str) -> bool:
    """True if ``func`` is registered in ``slot`` in the same translation unit.

    Argument Clinic emits ``foo_new_impl`` for a slot registered as
    ``foo_new``, so the ``_impl`` suffix is stripped before the second test.
    """
    registered = slots.get(slot, ())
    name = func["name"]
    if name in registered:
        return True
    return name.endswith("_impl") and name[: -len("_impl")] in registered


# ---------------------------------------------------------------------------
# Position helpers
# ---------------------------------------------------------------------------


def _iter_calls(text: str, start: int = 0, end: int | None = None):
    """Yield ``(callee, args_text, start, end)`` for every call in the range.

    Written by hand rather than with one regex because a regex that consumes
    ``if (PyModule_Add(m, "x", r) < 0)`` as a single match never sees the
    inner call, which silently hid every ownership transfer written inside an
    ``if`` condition.
    """
    end = len(text) if end is None else end
    pos = start
    while pos < end:
        m = re.compile(r"\b([A-Za-z_]\w*)\s*\(").search(text, pos, end)
        if m is None:
            return
        callee = m.group(1)
        depth = 1
        i = m.end()
        while i < len(text) and depth:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        if callee not in _SKIP_NAMES:
            yield callee, text[m.end() : i - 1], m.start(), i
        pos = m.end()


def _is_local_assignment(clean: str, start: int) -> bool:
    """True if the assignment at ``start`` binds a plain local variable.

    ``obj->m = New()`` and ``*out = New()`` hand ownership somewhere the
    scanner cannot follow, so they are not leaks.  ``PyObject *x = New()`` is
    a declaration and *is* a local bind -- the leading ``*`` belongs to the
    type, not to a dereference, which is what tells the two apart.
    """
    i = start - 1
    while i >= 0 and clean[i] in " \t":
        i -= 1
    if i < 0:
        return True
    if clean[i] in ">.":
        return False
    if clean[i] != "*":
        return True
    # A `*` — declaration only if a type name precedes it.
    i -= 1
    while i >= 0 and clean[i] in " \t*":
        i -= 1
    return i >= 0 and (clean[i].isalnum() or clean[i] == "_")


def _declared_locals(func: dict) -> set[str]:
    """Names declared as locals or parameters of ``func``.

    Anything else assigned in the body is a file-scope static or a module
    global.  CPython's module-init functions park objects in those on purpose
    and never release them -- process-lifetime by design, not a leak.
    """
    names: set[str] = set()
    for m in re.finditer(r"\**\s*([A-Za-z_]\w*)\s*(?:,|\)|$)", func.get("params", "")):
        names.add(m.group(1))
    decl = re.compile(
        r"^[ \t]*(?:const\s+|static\s+|register\s+|volatile\s+|unsigned\s+)*"
        r"(?:struct\s+|union\s+|enum\s+)?[A-Za-z_]\w*\s+([*\w\s,\[\]]+?)\s*[=;]",
        re.MULTILINE,
    )
    for m in decl.finditer(func["body"]):
        for part in m.group(1).split(","):
            ident = re.search(r"([A-Za-z_]\w*)", part.replace("*", " "))
            if ident:
                names.add(ident.group(1))
    return names


def _goto_guarded_by(clean: str, goto_pos: int, var: str) -> bool:
    """True if the ``goto`` sits inside a block guarded by a test of ``var``.

    ``PyObject *c = PyCapsule_New(...); if (c == NULL) { goto error; }`` has an
    error path, but ``c`` is NULL on it, so it cannot leak there.  A ``goto``
    guarded by some *other* variable's test is a genuine live path.
    """
    depth = 0
    for i in range(goto_pos - 1, -1, -1):
        ch = clean[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth == 0:
                # The controlling condition only: back up to the end of the
                # previous statement, not into unrelated earlier lines.
                start = i
                while start > 0 and clean[start - 1] not in ";{}":
                    start -= 1
                head = clean[start:i]
                return re.search(rf"\b{re.escape(var)}\b", head) is not None
            depth -= 1
    return False


def _line_at(func: dict, text: str, pos: int) -> int:
    """Absolute 1-based source line for offset ``pos`` inside ``text``."""
    return func["body_start_line"] + text.count("\n", 0, pos)


def _block_end(text: str, start: int) -> int:
    """Index at which the block enclosing ``start`` closes."""
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            if depth == 0:
                return i
            depth -= 1
    return len(text)


_LOOP_HEADER_RE = re.compile(r"\b(?:for|while)\s*\(")


def _enclosing_loop_end(text: str, pos: int) -> int | None:
    """End offset of the innermost ``for``/``while`` body containing ``pos``.

    Loop-carried exposure is why ``batched_next`` is invisible to a rule that
    only looks for a use positioned *after* a call: the borrowed local appears
    exactly once textually, and the danger is that iteration N+1's use follows
    iteration N's call.  Widening the search window to the end of the enclosing
    loop is what makes the two meet.
    """
    best: int | None = None
    for m in _LOOP_HEADER_RE.finditer(text, 0, pos):
        brace = text.find("{", m.end())
        if brace == -1 or brace > pos:
            continue
        # ``brace + 1``: _block_end reports where the block *enclosing* its
        # start index closes, so handing it the ``{`` itself starts one level
        # too deep and it returns the end of the block *outside* the loop.
        # In hackcheck_unlocked that made the first loop's window run to the
        # end of the second one, importing a PyErr_Format from a branch the
        # first loop cannot reach.
        end = _block_end(text, brace + 1)
        if end > pos and (best is None or end < best):
            best = end
    return best


def _same_block_region(text: str, start: int) -> int:
    """End of the straight-line region following ``start``.

    Stops at the end of the enclosing block, at the first control-flow
    terminator that is not nested inside a deeper block, or at a preprocessor
    branch boundary -- so anything found in between is genuinely reached after
    ``start``.
    """
    end = _block_end(text, start)
    for boundary in (_PREPROC_BRANCH_RE, _GOTO_LABEL_RE):
        m = boundary.search(text, start, end)
        if m:
            end = m.start()
    depth = 0
    i = start
    while i < end:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth == 0 and (ch.isalpha() or ch == "_"):
            m = _BLOCK_TERMINATOR_RE.match(text, i)
            if m:
                return m.end()
            m2 = _IDENT_RE.match(text, i)
            if m2:
                i = m2.end()
                continue
        i += 1
    return end


def _uses_of(
    text: str, start: int, end: int, names: set[str]
) -> tuple[int, str] | None:
    """Find the first *dereferencing* use of any name in ``names``.

    A use is a ``->``/``[]`` access or being handed to a call that is not one
    of the refcount / type-check macros.  A NULL comparison or a re-assignment
    is not a use.
    """
    for name in names:
        deref = re.compile(rf"\b{re.escape(name)}\s*(?:->|\[)")
        m = deref.search(text, start, end)
        if m:
            return m.start(), name
    for callee, args, pos, call_end in _iter_calls(text, start, end):
        # The whole call must fit inside the region: a call that merely starts
        # here and runs past the boundary (an out-parameter re-binding, say)
        # is not something that happens before the boundary.
        if call_end > end or _NON_DEREF_CALL_RE.match(callee):
            continue
        # Only *bare* identifiers count.  `self->last` must not read as a use
        # of a local named `last`.
        for ident in _BARE_IDENT_RE.findall(args):
            if ident in names:
                return pos, ident
    return None


def _alias_groups(clean: str) -> dict[str, set[str]]:
    """Map each variable to the set of variables it is aliased with.

    Only ``A = B = expr`` chains count -- that is the shape that made
    ``_Py_subs_parameters`` invisible to every earlier rule.
    """
    groups: dict[str, set[str]] = {}
    for m in _CHAIN_ASSIGN_RE.finditer(clean):
        a, b = m.group(1), m.group(2)
        merged = groups.get(a, {a}) | groups.get(b, {b})
        for name in merged:
            groups[name] = merged
    return groups


def _reassigned_before(clean: str, name: str, start: int, end: int) -> int | None:
    """Offset of the first re-binding of ``name`` in the region, if any.

    Counts ``&name`` handed to a call: CPython's ``PySys_GetOptionalAttr`` /
    ``_PyErr_Fetch`` style out-parameters overwrite the variable, so a
    ``Py_XDECREF(v)`` before such a call is releasing the *previous* value.
    """
    direct = re.compile(rf"(?<![\w.>*=!<])\b{re.escape(name)}\s*=(?!=)").search(
        clean, start, end
    )
    outparam = _outparam_rebind_re(name).search(clean, start, end)
    # A nested re-declaration shadows the outer variable entirely
    # (`Py_XDECREF(loader); if (...) { PyObject *loader = ...; }`).
    shadow = re.compile(rf"\*\s*{re.escape(name)}\s*=(?!=)").search(clean, start, end)
    # SCREAMING_CASE macros routinely assign their first argument
    # (`ASSIGN_PTR(obj, ...)` in Modules/_decimal).  Treat that as a re-bind.
    macro = re.compile(
        rf"\b[A-Z][A-Z0-9_]{{2,}}\s*\(\s*{re.escape(name)}\s*[,)]"
    ).search(clean, start, end)
    positions = [m.start() for m in (direct, outparam, macro, shadow) if m is not None]
    return min(positions) if positions else None


# ---------------------------------------------------------------------------
# Rule: stale_slot_decref
# ---------------------------------------------------------------------------


def check_stale_slot_decref(func: dict) -> list[dict]:
    """``x = obj->fld`` ... Python-reaching call ... ``obj->fld = NULL;
    Py_DECREF(x);``

    The slot is cleared and the *stale local* is released.  If the intervening
    call re-entered and already took this branch, the single reference is
    dropped twice.  ``Py_CLEAR(obj->fld)`` re-reads the field and is a no-op
    the second time, which is exactly what the guarded twin
    ``calliter_iternext`` does.
    """
    clean = strip_comments_and_strings(func["body"])
    findings: list[dict] = []
    seen: set[int] = set()

    for load in _SLOT_LOAD_RE.finditer(clean):
        local, obj, fld = load.group(1), load.group(2), load.group(3)
        if local == obj:
            continue
        null_store = re.compile(
            rf"\b{re.escape(obj)}\s*->\s*{re.escape(fld)}\s*=\s*NULL\s*;"
        )
        for store in null_store.finditer(clean, load.end()):
            reaching = python_reaching_calls(clean, load.end(), store.start())
            if not reaching:
                continue
            # An INCREF in between means the local owns a reference of its own.
            if any(
                m.group(2) == local
                for m in _INCREF_RE.finditer(clean, load.end(), store.start())
            ):
                continue
            # A re-load of the same field before the store makes it non-stale.
            if _reassigned_before(clean, local, load.end(), store.start()):
                continue
            drop = _BARE_DECREF_RE.search(clean, store.end())
            if drop is None or drop.group(2) != local:
                continue
            store_line = _line_at(func, clean, store.start())
            drop_line = _line_at(func, clean, drop.start())
            if drop_line - store_line > 3:
                continue
            if drop_line in seen:
                continue
            seen.add(drop_line)
            only_refcount = all(name in _REFCOUNT_ONLY_REACHING for name in reaching)
            findings.append(
                {
                    "type": "stale_slot_decref",
                    "api_call": reaching[0],
                    "variable": local,
                    "line": drop_line,
                    "detail": (
                        f"'{local}' was loaded from {obj}->{fld} before "
                        f"{', '.join(sorted(set(reaching)))} ran, which can execute "
                        f"arbitrary Python; a re-entrant call can clear {obj}->{fld} "
                        f"and drop the reference first, so this "
                        f"{drop.group(1)}({local}) drops it a second time. "
                        f"Use Py_CLEAR({obj}->{fld}) instead of the "
                        f"{obj}->{fld} = NULL / {drop.group(1)}({local}) pair"
                    ),
                    "confidence": "low" if only_refcount else "high",
                }
            )
            break
    return findings


# ---------------------------------------------------------------------------
# Rule: owner_freed_before_use
# ---------------------------------------------------------------------------


def check_owner_freed_before_use(func: dict) -> list[dict]:
    """``Py_DECREF(V);`` then a dereferencing read of ``V`` or an alias of it.

    Aliases matter: ``args = tuple_args = PySequence_Tuple(args)`` makes the
    two names one object, and ``Py_XDECREF(tuple_args)`` frees what a later
    ``PyTuple_GET_ITEM(args, i)`` reads.
    """
    clean = strip_comments_and_strings(func["body"])
    aliases = _alias_groups(clean)
    findings: list[dict] = []
    seen: set[int] = set()

    for m in _BARE_DECREF_RE.finditer(clean):
        var = m.group(2)
        group = set(aliases.get(var, {var}))
        # A reference this function acquired itself (Py_INCREF on a caller's
        # object) is being handed back, not necessarily released: the caller
        # still owns one.  That is the "borrowed under a known-live owner"
        # class, not a use-after-free.
        if any(
            inc.group(2) in group for inc in _INCREF_RE.finditer(clean, 0, m.start())
        ):
            continue
        # Ownership already handed to a module/container that keeps it alive.
        if any(
            set(_IDENT_RE.findall(pub.group(2))) & group
            for pub in _PUBLISH_CALL_RE.finditer(clean, 0, m.start())
        ):
            continue
        region_end = _same_block_region(clean, m.end())
        # A re-binding of any group member ends the danger window.
        for name in group:
            reassign = _reassigned_before(clean, name, m.end(), region_end)
            if reassign is not None:
                region_end = min(region_end, reassign)
        # Py_CLEAR NULLs its operand, so a later read of *that* variable is a
        # NULL read, not a dangling one.  Aliases are still exposed.
        candidates = group - {var} if m.group(1) == "Py_CLEAR" else group
        if not candidates:
            continue
        hit = _uses_of(clean, m.end(), region_end, candidates)
        if hit is None:
            continue
        pos, used = hit
        line = _line_at(func, clean, pos)
        if line in seen:
            continue
        seen.add(line)
        via_alias = used != var
        findings.append(
            {
                "type": "owner_freed_before_use",
                "api_call": m.group(1),
                "variable": var,
                "line": line,
                "detail": (
                    f"{m.group(1)}({var}) at line "
                    f"{_line_at(func, clean, m.start())} may release the last "
                    f"reference; '{used}' is then dereferenced here"
                    + (
                        f" -- '{used}' and '{var}' are aliases "
                        f"(assigned together in one '=' chain)"
                        if via_alias
                        else ""
                    )
                    + ". Hoist the read above the release, or hold a reference "
                    "across it"
                ),
                "confidence": "high" if via_alias else "medium",
            }
        )
    return findings


# ---------------------------------------------------------------------------
# Rule: borrowed_ref_across_call
# ---------------------------------------------------------------------------


def check_borrowed_ref_across_call(
    func: dict,
    *,
    skip_lines: set[int] | None = None,
) -> list[dict]:
    """A borrowed pointer is *released* after a Python-reaching call.

    Narrow on purpose.  "Borrowed pointer used at all after a call that can run
    Python" is unresolvable without dataflow and fires thousands of times on
    CPython's own code, almost always under a provably live owner.  Releasing
    ownership through a borrowed pointer is the shape that is a bug regardless
    of who else holds a reference, and re-entrancy is what makes it reachable.
    """
    clean = strip_comments_and_strings(func["body"])
    skip = skip_lines or set()
    findings: list[dict] = []
    seen: set[int] = set()

    loads: list[tuple[int, str, str, str | None]] = []
    for m in _SLOT_LOAD_RE.finditer(clean):
        if m.group(1) != m.group(2):
            loads.append(
                (
                    m.end(),
                    m.group(1),
                    f"{m.group(2)}->{m.group(3)}",
                    rf"\b{re.escape(m.group(2))}\s*->\s*{re.escape(m.group(3))}\s*=(?!=)",
                )
            )
    for m in _BORROWED_CALL_LOAD_RE.finditer(clean):
        loads.append((m.end(), m.group(1), m.group(2), None))

    for load_end, local, source, slot_store in loads:
        releases = [
            (r.start(), f"{r.group(1)}({local})")
            for r in _BARE_DECREF_RE.finditer(clean, load_end)
            if r.group(2) == local
        ] + [
            (r.start(), f"{r.group(1)}(..., {local})")
            for r in _STEAL_CALL_RE.finditer(clean, load_end)
            if r.group(2) == local
        ]
        if not releases:
            continue
        rel_pos, rel_text = min(releases)
        reaching = python_reaching_calls(clean, load_end, rel_pos)
        reaching = [n for n in reaching if n not in _REFCOUNT_ONLY_REACHING]
        if not reaching:
            continue
        if any(
            m.group(2) == local for m in _INCREF_RE.finditer(clean, load_end, rel_pos)
        ):
            continue
        # The INCREF is often written against the *source* expression rather
        # than the destination local (`Py_INCREF(lz->lz_attr); x = lz->lz_attr;`),
        # which makes the local an owner.  Search the whole body: if this
        # function ever takes a reference on that source, it owns one.
        source_pat = r"\s*->\s*".join(re.escape(part) for part in source.split("->"))
        if re.search(
            r"\b(?:Py_X?INCREF|Py_X?NewRef)\s*\(\s*" + source_pat + r"\s*\)",
            clean,
        ):
            continue
        if _reassigned_before(clean, local, load_end, rel_pos) is not None:
            continue
        # If the slot itself was overwritten before the release, the local is
        # the legitimate sole owner of the old value and dropping it is
        # correct (defaultdict's `olddefault` swap).  The dangerous variant —
        # the slot cleared to NULL and the stale local dropped — is
        # stale_slot_decref's, which gates on proximity as well.
        if slot_store and re.search(slot_store, clean[load_end:rel_pos]):
            continue
        line = _line_at(func, clean, rel_pos)
        if line in seen or line in skip:
            continue
        seen.add(line)
        findings.append(
            {
                "type": "borrowed_ref_across_call",
                "api_call": reaching[0],
                "variable": local,
                "line": line,
                "detail": (
                    f"'{local}' is a borrowed reference from {source}; "
                    f"{', '.join(sorted(set(reaching)))} can run arbitrary Python "
                    f"between the read and this {rel_text}, so ownership is "
                    f"released through a pointer this function never owned and "
                    f"may no longer be valid. INCREF before the call, or re-read "
                    f"the owner after it"
                ),
                "confidence": "medium",
            }
        )
    return findings


# ---------------------------------------------------------------------------
# False-positive gate: type-constrained operands
# ---------------------------------------------------------------------------
#
# ``PyNumber_Add`` / ``PyObject_RichCompare`` are in PYTHON_REACHING_APIS, but
# if every operand is provably a concrete builtin ``int`` for the lifetime of
# the field, the dispatch resolves to ``long_add`` and **no user code runs**.
#
# ``Objects/enumobject.c:196`` ``increment_longindex_lock_held`` is the
# exemplar: ``en->one`` is ``_PyLong_GetOne()`` and ``en_longindex`` is only
# ever a ``PyLong``.  Its sibling ``Modules/itertoolsmodule.c``
# ``count_nextlong`` is textually identical *and is a real, reproduced UAF*,
# because ``lz->long_step`` comes straight from a constructor parameter that is
# only ``PyLong_Check``-ed on the fast path -- and ``count_nextlong`` is the
# slow path.  The discriminator implemented below is exactly that asymmetry: a
# parameter counts as int-pinned only when the function *coerces it through an
# int-producing conversion of itself* (``start = PyNumber_Index(start)``), not
# when it merely receives a default (``long_step = _PyLong_GetOne()``).

_INT_PRODUCER_PREFIXES = (
    "PyLong_From",
    "_PyLong_From",
    "PyLong_Get",
    "_PyLong_Get",
    "PyNumber_Index",
    "_PyNumber_Index",
    "PyNumber_Long",
)

# Protocol calls whose dispatch is pinned once every operand's type is known.
_TYPE_PINNABLE_CALLS = frozenset(
    {
        "PyNumber_Add",
        "PyNumber_Subtract",
        "PyNumber_Multiply",
        "PyNumber_FloorDivide",
        "PyNumber_TrueDivide",
        "PyNumber_Remainder",
        "PyNumber_Divmod",
        "PyNumber_Power",
        "PyNumber_Lshift",
        "PyNumber_Rshift",
        "PyNumber_And",
        "PyNumber_Xor",
        "PyNumber_Or",
        "PyNumber_InPlaceAdd",
        "PyNumber_InPlaceSubtract",
        "PyObject_RichCompare",
        "PyObject_RichCompareBool",
    }
)

_CALL_EXPR_RE = re.compile(
    r"^\s*(?:\(\s*[\w\s*]+\)\s*)?([A-Za-z_]\w*)\s*\((.*)\)\s*$", re.DOTALL
)
_FIELD_EXPR_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*->\s*([A-Za-z_]\w*)\s*$")
_NAME_EXPR_RE = re.compile(r"^\s*&?\s*([A-Za-z_]\w*)\s*$")
_INT_LITERAL_RE = re.compile(r"^\s*[-+]?(?:\d+[UuLl]*|0[xX][0-9a-fA-F]+[UuLl]*)\s*$")


def _split_args(text: str) -> list[str]:
    """Split a call's argument text on top-level commas."""
    args: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(current))
            current = []
            continue
        current.append(ch)
    if "".join(current).strip():
        args.append("".join(current))
    return args


def _is_int_producer(name: str) -> bool:
    return any(name.startswith(p) for p in _INT_PRODUCER_PREFIXES)


class FileRefContext:
    """Per-file context the borrowed-ref rules need beyond one function body.

    Two of the rules reason about a struct *field*, and a field is written from
    several functions -- typically the constructor writes it and the iterator
    steps it.  That is file scope, not function scope, so the analysis needs
    the whole translation unit.
    """

    def __init__(self, source: str, functions: list[dict]) -> None:
        self.clean = strip_comments_and_strings(source)
        self.functions = functions
        self._pin_cache: dict[tuple, bool] = {}
        self._reaching: set[str] | None = None
        self._twin_cache: dict[str, int | None] = {}
        self._accessors: dict[str, str] | None = None

    # -- same-file transitive reach ------------------------------------------
    #
    # ``type_ready_inherit`` reaches a user ``__eq__`` through
    # ``inherit_slots`` -> ``overrides_hash`` -> ``PyDict_Contains``.  A rule
    # that only knows the API table sees an ordinary-looking local call and
    # concludes the window is quiet, which is how a reproduced
    # use-after-free reads as clean code.
    #
    # Only functions *defined in this file* propagate.  An unresolved external
    # call stays unknown rather than being assumed dangerous: assuming would
    # make every window reaching and the gate would stop gating.

    def transitively_reaching(self) -> set[str]:
        """Same-file functions that can reach arbitrary Python, transitively."""
        if self._reaching is not None:
            return self._reaching
        callees: dict[str, set[str]] = {}
        reaching: set[str] = set()
        for func in self.functions:
            body = strip_comments_and_strings(func["body"])
            if reaching_calls_with_slots(body, 0, len(body)):
                reaching.add(func["name"])
            callees[func["name"]] = {c for c, _a, _s, _e in _iter_calls(body)}
        changed = True
        while changed:
            changed = False
            for name, called in callees.items():
                if name not in reaching and called & reaching:
                    reaching.add(name)
                    changed = True
        self._reaching = reaching
        return reaching

    def reaching_in(self, clean_body: str, start: int, end: int) -> list[str]:
        """``reaching_calls_with_slots`` plus same-file helpers that reach."""
        names = reaching_calls_with_slots(clean_body, start, end)
        if start >= end:
            return names
        transitive = self.transitively_reaching()
        for callee, _args, _pos, call_end in _iter_calls(clean_body, start, end):
            if call_end <= end and callee in transitive:
                names.append(f"{callee}() [same-file helper, reaches Python]")
        return names

    # -- borrowing field accessors ------------------------------------------

    def field_accessors(self, source: str) -> dict[str, str]:
        if self._accessors is None:
            self._accessors = discover_field_accessors(source, self.functions)
        return self._accessors

    def field_accessor_twin(self, accessor: str) -> int | None:
        """Line of a site in this file that takes a strong ref on the result.

        Both spellings: ``Py_XNewRef(lookup_tp_mro(type))`` wrapped around the
        call, and the two-step ``res = lookup_tp_bases(self); Py_INCREF(res);``
        that ``_PyType_GetBases`` uses.
        """
        if accessor in self._twin_cache:
            return self._twin_cache[accessor]
        wrapped = re.compile(
            r"\bPy_X?(?:NewRef|INCREF)\s*\(\s*" + re.escape(accessor) + r"\s*\("
        )
        twin: int | None = None
        for func in self.functions:
            body = strip_comments_and_strings(func["body"])
            m = wrapped.search(body)
            if m:
                twin = _line_at(func, body, m.start())
                break
            for load_end, local, _acc in _field_accessor_loads(body, (accessor,)):
                if _strong_ref_re(local).search(body[load_end:]):
                    twin = _line_at(func, body, load_end)
                    break
            if twin is not None:
                break
        self._twin_cache[accessor] = twin
        return twin

    def _function_at_line(self, line: int) -> dict | None:
        for func in self.functions:
            if func["body_start_line"] <= line <= func["end_line"]:
                return func
        return None

    def _field_assignments(self, field: str) -> list[tuple[str, dict | None]]:
        """Every ``<anything>->field = RHS;`` in the file, with its function."""
        out: list[tuple[str, dict | None]] = []
        pattern = re.compile(
            rf"[A-Za-z_]\w*\s*->\s*{re.escape(field)}\s*=(?!=)([^;]*);"
        )
        for m in pattern.finditer(self.clean):
            line = self.clean.count("\n", 0, m.start()) + 1
            out.append((m.group(1), self._function_at_line(line)))
        return out

    def is_int_pinned(self, expr: str, func: dict | None, seen=None) -> bool:
        """True if ``expr`` can only ever hold a concrete builtin ``int``."""
        seen = set() if seen is None else seen
        expr = expr.strip().rstrip(";").strip()
        if not expr:
            return False
        if expr == "NULL" or _INT_LITERAL_RE.match(expr):
            return True

        field = _FIELD_EXPR_RE.match(expr)
        if field:
            key = ("field", field.group(2))
            if key in seen:
                # A field assigned from a stepped-up value of itself: the
                # cycle carries no new type information, so it neither
                # proves nor disproves.  Treat it as neutral.
                return True
            seen.add(key)
            assignments = self._field_assignments(field.group(2))
            if not assignments:
                return False
            return all(
                self.is_int_pinned(rhs, owner, seen) for rhs, owner in assignments
            )

        call = _CALL_EXPR_RE.match(expr)
        if call:
            name, args = call.group(1), call.group(2)
            if _is_int_producer(name):
                return True
            if name in _TYPE_PINNABLE_CALLS:
                return all(self.is_int_pinned(a, func, seen) for a in _split_args(args))
            return False

        name_m = _NAME_EXPR_RE.match(expr)
        if name_m and func is not None:
            name = name_m.group(1)
            key = ("name", func["name"], name)
            if key in seen:
                return True
            seen.add(key)
            body = strip_comments_and_strings(func["body"])
            params = {
                m.group(1)
                for m in re.finditer(
                    r"\**\s*([A-Za-z_]\w*)\s*(?:,|\)|$)", func.get("params", "")
                )
            }
            # Both spellings: a plain re-assignment and a declaration with
            # initialiser.  Missing the declaration form (`PyObject *x = ...`)
            # makes every local look unconstrained, which silently disables
            # the whole gate.
            plain_re = re.compile(
                rf"(?<![\w.>*=!<])\b{re.escape(name)}\s*=(?!=)([^;]*);"
            )
            decl_re = re.compile(
                rf"(?<![\w.>])[A-Za-z_]\w*\s+\*+\s*{re.escape(name)}\s*=(?!=)([^;]*);"
            )
            assignments = [m.group(1) for m in plain_re.finditer(body)]
            assignments += [m.group(1) for m in decl_re.finditer(body)]
            if name in params:
                # A parameter arrives holding whatever the caller passed.  Only
                # a *self-coercion* -- an int-producing call that consumes the
                # parameter -- proves its type.  A plain default assignment
                # (`long_step = _PyLong_GetOne();`) says nothing about the
                # value the caller actually supplied.
                for rhs in assignments:
                    call_m = _CALL_EXPR_RE.match(rhs)
                    if (
                        call_m
                        and _is_int_producer(call_m.group(1))
                        and re.search(rf"\b{re.escape(name)}\b", call_m.group(2))
                    ):
                        return True
                return False
            if not assignments:
                return False
            return all(self.is_int_pinned(rhs, func, seen) for rhs in assignments)
        return False

    def call_is_type_pinned(self, func: dict, clean: str, start: int, end: int) -> bool:
        """True if every protocol call in the window is type-pinned to a builtin.

        Suppression only, and deliberately narrow: it fires when *all* the
        Python-reaching calls in the window are binary numeric / comparison
        protocol calls whose operands are provably ``int``.  Anything else --
        including one unpinnable operand -- leaves the window dangerous.
        """
        found = False
        for callee, args, pos, _end in _iter_calls(clean, start, end):
            if callee not in _TYPE_PINNABLE_CALLS:
                continue
            found = True
            if not all(self.is_int_pinned(arg, func) for arg in _split_args(args)):
                return False
            del pos
        return found


# ---------------------------------------------------------------------------
# Rule: slot_transfer_across_call  (the escape hazard)
# ---------------------------------------------------------------------------


_ESCAPE_RETURN_RE = re.compile(r"\breturn\s+([A-Za-z_]\w*)\s*;")
_ESCAPE_STORE_RE = re.compile(
    r"(?:\*\s*[A-Za-z_]\w*|[A-Za-z_]\w*\s*->\s*[A-Za-z_]\w*)"
    r"\s*=\s*([A-Za-z_]\w*)\s*;"
)


def _borrowed_slot_loads(clean: str) -> list[tuple[int, str, str, str]]:
    """``local = owner->field;`` loads, as ``(end, local, owner, field)``.

    ``local == owner`` is excluded (``it = it->next`` is a walk, not a borrow).
    A "the local must be named differently from the field" filter is
    deliberately *not* applied: ``it = lz->it`` / ``seq = it->it_seq`` is the
    commonest spelling in CPython precisely because the local is named after
    the field, and filtering on it drops the real hits.
    """
    return [
        (m.end(), m.group(1), m.group(2), m.group(3))
        for m in _SLOT_LOAD_RE.finditer(clean)
        if m.group(1) != m.group(2)
    ]


def _owns_a_reference(clean: str, local: str, owner: str, field: str) -> bool:
    """True if the function takes a reference on the local or on the slot."""
    if re.search(
        r"\b(?:Py_X?INCREF|Py_X?NewRef|_Py_X?NewRef)\s*\(\s*"
        + re.escape(local)
        + r"\s*\)",
        clean,
    ):
        return True
    slot = re.escape(owner) + r"\s*->\s*" + re.escape(field)
    return bool(
        re.search(
            r"\b(?:Py_X?INCREF|Py_X?NewRef|_Py_X?NewRef)\s*\(\s*" + slot + r"\s*\)",
            clean,
        )
    )


def check_slot_transfer_across_call(
    func: dict,
    ctx: "FileRefContext",
) -> list[dict]:
    """``local = owner->field`` ... Python runs ... ``owner->field = new`` ...
    ``return local``.

    The "we hold one reference to the old value; we'll either return it or
    keep it in the slot" transfer idiom, performed across a window in which a
    re-entrant call can perform the *same* transfer -- so two callers each
    believe they own the single reference.

    Ordering gate: the overwrite must come **after** the Python-reaching call.
    If it precedes the call, the transfer completed while this thread was
    alone and the local is the legitimate sole owner.

    True positive: ``Modules/itertoolsmodule.c`` ``count_nextlong``
    (reproduced: ASan heap-use-after-free, and the freed counter recycled as a
    ``dict``).  Guarded twin / suppressed FP: ``Objects/enumobject.c``
    ``increment_longindex_lock_held``, structurally identical but
    type-constrained to ``int``.
    """
    clean = strip_comments_and_strings(func["body"])
    findings: list[dict] = []
    seen: set[int] = set()

    for load_end, local, owner, field in _borrowed_slot_loads(clean):
        if _owns_a_reference(clean, local, owner, field):
            continue
        if _reassigned_before(clean, local, load_end, len(clean)) is not None:
            continue
        escapes = [
            m.start()
            for m in _ESCAPE_RETURN_RE.finditer(clean, load_end)
            if m.group(1) == local
        ] + [
            m.start()
            for m in _ESCAPE_STORE_RE.finditer(clean, load_end)
            if m.group(1) == local
        ]
        if not escapes:
            continue
        escape_pos = min(escapes)
        slot_store = re.compile(
            re.escape(owner) + r"\s*->\s*" + re.escape(field) + r"\s*=(?!=)"
        )
        overwrites = [
            m.start()
            for m in slot_store.finditer(clean, load_end)
            if m.start() < escape_pos
        ]
        if not overwrites:
            continue
        window_end = max(overwrites)
        reaching = reaching_calls_with_slots(clean, load_end, window_end)
        if not reaching:
            continue
        if ctx.call_is_type_pinned(func, clean, load_end, window_end):
            # Every protocol call in the window is pinned to a builtin int:
            # the dispatch resolves to a C slot and no user code runs
            # (Objects/enumobject.c increment_longindex_lock_held).
            continue
        line = _line_at(func, clean, load_end)
        if line in seen:
            continue
        seen.add(line)
        findings.append(
            {
                "type": "slot_transfer_across_call",
                "api_call": reaching[0],
                "variable": local,
                "line": line,
                "escape_line": _line_at(func, clean, escape_pos),
                "source": f"{owner}->{field}",
                "detail": (
                    f"'{local}' is borrowed from {owner}->{field} here; "
                    f"{', '.join(sorted(set(reaching)))} can run arbitrary Python "
                    f"before {owner}->{field} is overwritten at line "
                    f"{_line_at(func, clean, window_end)}, and '{local}' then "
                    f"escapes at line {_line_at(func, clean, escape_pos)}. A "
                    f"re-entrant call performs the same transfer, so the single "
                    f"reference is handed to two owners. Take a reference on "
                    f"{owner}->{field} before the call, or re-read the slot after it"
                ),
                "confidence": "medium",
            }
        )
    return findings


# ---------------------------------------------------------------------------
# Rule: stale_slot_use  (the deref / call hazard)
# ---------------------------------------------------------------------------


def check_stale_slot_use(
    func: dict,
    ctx: "FileRefContext",
    *,
    skip_lines: set[int] | None = None,
) -> list[dict]:
    """``local = owner->field`` ... Python runs ... ``Py_CLEAR(owner->field)``
    ... ``local`` dereferenced or called.

    A re-entrant call reaches the clear and drops the only reference; the outer
    frame then *calls through* the freed object.  Worse than a double-DECREF:
    ``slot_tp_iternext`` reads ``Py_TYPE(self)`` out of the freed block.

    True positives: ``Modules/itertoolsmodule.c`` ``batched_next`` (:210) and
    ``islice_next`` (:1711), both reproduced as ASan heap-use-after-free, and
    ``Objects/iterobject.c`` ``iter_iternext`` (CPY-0003).

    Three gates carry the precision, all measured:
    (i) the clear must be reachable *after* a Python-reaching call -- a clear
    that precedes it is a completed ownership transfer (``_tkinter.c``
    ``TimerHandler``, ``_elementtree.c`` ``elementiter_next``);
    (ii) a local re-read from the slot after the call is the guarded twin
    (``pairwise_next:364``) and must suppress;
    (iii) ``_reassigned_before``.
    """
    clean = strip_comments_and_strings(func["body"])
    skip = skip_lines or set()
    findings: list[dict] = []
    seen: set[int] = set()

    for load_end, local, owner, field in _borrowed_slot_loads(clean):
        if _owns_a_reference(clean, local, owner, field):
            continue
        slot = re.escape(owner) + r"\s*->\s*" + re.escape(field)
        cleared = re.compile(rf"(?:Py_CLEAR\s*\(\s*{slot}\s*\)|{slot}\s*=\s*NULL)")
        clears = [m.start() for m in cleared.finditer(clean, load_end)]
        if not clears:
            continue
        if _reassigned_before(clean, local, load_end, len(clean)) is not None:
            continue
        if not reaching_calls_with_slots(clean, load_end, min(clears)):
            continue
        # A re-read of the slot into the same local is the pairwise_next guard.
        reread = re.compile(
            r"(?<![\w.>*=!<])\b" + re.escape(local) + r"\s*=\s*"
            r"(?:\(\s*[\w\s*]+\)\s*)?" + slot
        )
        if len(reread.findall(clean)) > 1:
            continue
        uses = [
            m.start()
            for m in re.finditer(
                r"\b" + re.escape(local) + r"\s*->"
                r"|\(\s*\*?\s*[\w>.\-]*\)?\s*\(\s*" + re.escape(local) + r"\s*[,)]"
                r"|\b\w+\s*\(\s*" + re.escape(local) + r"\s*[,)]",
                clean,
            )
            if m.start() > load_end
        ]
        for use_pos in uses:
            reaching = reaching_calls_with_slots(clean, load_end, use_pos)
            if not reaching:
                # Loop-carried exposure: the use sits inside a loop, so
                # iteration N+1's use follows iteration N's call.
                loop_end = _enclosing_loop_end(clean, use_pos)
                if loop_end is not None:
                    reaching = reaching_calls_with_slots(clean, load_end, loop_end)
            if not reaching:
                continue
            if ctx.call_is_type_pinned(func, clean, load_end, use_pos):
                continue
            line = _line_at(func, clean, use_pos)
            if line in seen or line in skip:
                # The narrower stale_slot_decref / borrowed_ref_across_call
                # rules already own this line and name the exact fix; a second
                # finding at the same coordinates is noise, not recall.
                break
            seen.add(line)
            findings.append(
                {
                    "type": "stale_slot_use",
                    "api_call": reaching[0],
                    "variable": local,
                    "line": line,
                    "source": f"{owner}->{field}",
                    "load_line": _line_at(func, clean, load_end),
                    "detail": (
                        f"'{local}' is borrowed from {owner}->{field} at line "
                        f"{_line_at(func, clean, load_end)}; "
                        f"{', '.join(sorted(set(reaching)))} can run arbitrary "
                        f"Python, and a re-entrant call reaching the "
                        f"{owner}->{field} clear at line "
                        f"{_line_at(func, clean, min(clears))} frees the object "
                        f"'{local}' still points at -- which is then dereferenced "
                        f"or called here. Re-read {owner}->{field} after the call "
                        f"and bail on NULL (the pairwise_next pattern), or hold a "
                        f"strong reference for the duration"
                    ),
                    "confidence": "high",
                }
            )
            break
    return findings


# ---------------------------------------------------------------------------
# Rule: borrowed_field_deref_across_call  (the read-only hazard)
# ---------------------------------------------------------------------------
#
# Every rule above anchors on a *release*: a ``Py_DECREF``, a slot clear, an
# ownership transfer out of the function.  ``Objects/typeobject.c`` contains
# two reproduced use-after-frees in which the borrowed local is only ever
# **read** -- nothing in the enclosing function releases anything, and the free
# happens frames away inside re-entrant Python.  The release-anchored rules
# reported 0 findings over 403 functions on that file.
#
# The load is not spelled ``local = owner->field`` either.  It goes through a
# tiny ``static inline`` accessor:
#
#     static inline PyObject *
#     lookup_tp_mro(PyTypeObject *self)
#     {
#         return self->tp_mro;          /* borrowed */
#     }
#
# so the accessor set is *discovered* from the file rather than tabulated --
# which is what makes the rule portable to the next file that wraps its slots.
#
# Four gates carry the precision, all measured on the 28 borrowed accessor
# loads in ``Objects/typeobject.c``:
#
# (i)   no strong reference taken on the local;
# (ii)  a Python-reaching call between the load and the use -- including one
#       reached through a same-file ``static`` helper, because ``inherit_slots``
#       is what dispatches the user ``__eq__`` in the ``type_ready_inherit``
#       finding, and including loop-carried exposure, because in
#       ``recurse_down_subclasses`` the guard test that runs Python sits *after*
#       the use in text order and *before* it in iteration order;
# (iii) the use must be a **dereference**.  ``lookup_tp_bases(type) ==
#       new_bases`` (:1957, :1993, :3667) is a deliberate re-entrancy check and
#       correct code; counting a pointer comparison as a use drowns the rule;
# (iv)  ``high`` only when a *guarded twin* exists in the same file -- a site
#       that takes a strong reference on the same accessor's result.  That one
#       test is what separates the findings from the clean borrowed-MRO loops.
#
# True positives: ``type_ready_inherit`` (:9332 load, :9336 use, CPY-0068) and
# ``recurse_down_subclasses`` (:12369 load, :12377 use, CPY-0069), both
# reproduced as ASan heap-use-after-free.  Guarded twins: ``_PyType_GetBases``
# (``Py_INCREF`` on the ``lookup_tp_bases`` result) and :3665
# (``Py_XNewRef(lookup_tp_mro(type))``).
#
# ``PyDict_Next`` is deliberately *not* in ``PYTHON_REACHING_APIS``: it walks
# the entry table by index and neither hashes nor compares, so it cannot reach
# a user ``__eq__``.  It is a *use* of the borrowed container, not a call that
# invalidates it.

_ANY_RETURN_RE = re.compile(r"\breturn\b([^;]*);")

# ``return X->field;`` / ``return (PyObject *)X->field;`` -- the whole body of a
# borrowing accessor.
_ACCESSOR_RETURN_RE = re.compile(
    r"^\s*(?:\(\s*[\w\s*]+\)\s*)?([A-Za-z_]\w*)\s*->\s*([A-Za-z_]\w*)\s*$"
)

_STRONG_REF_ANY_RE = re.compile(r"\bPy_X?(?:INCREF|NewRef)\b|\b_Py_X?NewRef\b")


def _strong_ref_re(name: str) -> re.Pattern:
    """``Py_INCREF(name)`` / ``Py_XNewRef(name)`` for one exact operand."""
    return re.compile(
        r"\b(?:Py_X?INCREF|Py_X?NewRef|_Py_X?NewRef)\s*\(\s*"
        + re.escape(name)
        + r"\s*\)"
    )


def discover_field_accessors(source: str, functions: list[dict]) -> dict[str, str]:
    """Map ``accessor name -> field name`` for borrowing field accessors.

    A borrowing accessor is a file-scope ``static`` function returning
    ``PyObject *`` whose every ``return`` hands back either a struct field or
    ``NULL``, and which takes no reference anywhere in its body.  On
    ``Objects/typeobject.c`` that discovers exactly ``lookup_tp_dict``,
    ``lookup_tp_bases``, ``lookup_tp_mro`` and ``lookup_tp_subclasses`` --
    including the two whose bodies have a static-builtin branch and therefore
    *two* field returns.

    Tabulating the four names instead would have worked on this one file and
    nowhere else; the shape is what generalises.
    """
    lines = source.split("\n")
    accessors: dict[str, str] = {}
    for func in functions:
        header = "\n".join(lines[func["start_line"] - 1 : func["body_start_line"] - 1])
        if "static" not in header or "PyObject" not in header or "*" not in header:
            continue
        body = strip_comments_and_strings(func["body"])
        if _STRONG_REF_ANY_RE.search(body):
            # It hands back a strong reference; borrowing is the whole point.
            continue
        fields: list[str] = []
        vouched = True
        for m in _ANY_RETURN_RE.finditer(body):
            expr = m.group(1).strip()
            if expr in ("", "NULL", "0"):
                continue
            fm = _ACCESSOR_RETURN_RE.match(expr)
            if fm is None:
                # A return this rule cannot vouch for: the function computes
                # something rather than forwarding a field.
                vouched = False
                break
            fields.append(fm.group(2))
        if not vouched or not fields:
            continue
        # The commonest field wins: `lookup_tp_subclasses` returns
        # `state->tp_subclasses` and `self->tp_subclasses`, the same field.
        accessors[func["name"]] = max(set(fields), key=fields.count)
    return accessors


def _accessor_load_re(accessors) -> re.Pattern:
    """``[Type *]local = ACCESSOR(...)`` for any of ``accessors``.

    An explicit optional declaration prefix rather than a ``(?<![\\w.>*])``
    lookbehind: the declaration form ``PyObject *mro = lookup_tp_mro(type);``
    puts a ``*`` immediately before the local, so the lookbehind the sibling
    rules use rejects the commonest spelling of the thing being looked for.
    """
    return re.compile(
        r"(?:^|[;{}\s])(?:(?:const\s+)?[A-Za-z_]\w*\s*\*+\s*)?([A-Za-z_]\w*)\s*=\s*"
        r"(?:\(\s*[\w\s*]+\)\s*)?("
        + "|".join(re.escape(a) for a in sorted(accessors, key=len, reverse=True))
        + r")\s*\(",
        re.MULTILINE,
    )


def _field_accessor_loads(clean: str, accessors) -> list[tuple[int, str, str]]:
    """``(end, local, accessor)`` for every borrowing-accessor load."""
    if not accessors:
        return []
    return [
        (m.end(), m.group(1), m.group(2))
        for m in _accessor_load_re(accessors).finditer(clean)
    ]


def _loop_scope_end(text: str, pos: int, *, load_end: int) -> int | None:
    """End of the innermost loop whose *header or body* contains ``pos``.

    Two differences from ``_enclosing_loop_end``:

    * ``pos`` may sit in the controlling expression.  ``_enclosing_loop_end``
      requires it to follow the opening brace, so a use written as
      ``while (PyDict_Next(d, ...))`` reads as "not in a loop" -- which is
      exactly the ``recurse_down_subclasses`` shape, where the invalidating
      call is the guard test further down the same body.
    * the loop must begin **at or after the load**.  A load *inside* the loop
      is refreshed on every iteration, so iteration N+1's use follows iteration
      N+1's load, not iteration N's call.  Without this, the ``dict =
      lookup_tp_dict(subclass)`` re-load in that same loop body reports as
      loop-carried when it is not.
    """
    best: int | None = None
    for m in _LOOP_HEADER_RE.finditer(text):
        if m.start() < load_end:
            continue
        close = _close_paren(text, m.end() - 1)
        if close is None:
            continue
        brace = text.find("{", close)
        if brace != -1 and not text[close + 1 : brace].strip():
            end = _block_end(text, brace + 1)
        else:
            semi = text.find(";", close)
            end = len(text) if semi == -1 else semi + 1
        if m.start() <= pos <= end and (best is None or end < best):
            best = end
    return best


def _deref_uses(text: str, start: int, end: int, name: str) -> list[int]:
    """Offsets of every *dereferencing* use of ``name`` in the region.

    ``->``/``[]`` access, or being handed to a call that is not a refcount or
    assertion macro.  A pointer comparison and a bare ``return name`` are not
    dereferences -- gate (iii).
    """
    out: set[int] = set()
    for m in re.finditer(rf"(?<![\w.>]){re.escape(name)}\s*(?:->|\[)", text):
        if start <= m.start() < end:
            out.add(m.start())
    for callee, args, pos, call_end in _iter_calls(text, start, end):
        if call_end > end or _NON_DEREF_CALL_RE.match(callee):
            continue
        if name in _BARE_IDENT_RE.findall(args):
            out.add(pos)
    return sorted(out)


def check_borrowed_field_deref(
    func: dict,
    ctx: "FileRefContext",
    *,
    accessors: dict[str, str],
    skip_lines: set[int] | None = None,
) -> list[dict]:
    """``local = lookup_field(obj)`` ... Python runs ... ``local`` dereferenced.

    The read-only borrowed-ref hazard.  See the block comment above for the
    four gates and the two reproduced true positives.
    """
    if not accessors:
        return []
    clean = strip_comments_and_strings(func["body"])
    skip = skip_lines or set()
    findings: list[dict] = []
    seen: set[int] = set()

    for load_end, local, accessor in _field_accessor_loads(clean, accessors):
        if _strong_ref_re(local).search(clean):
            continue
        rebound = _reassigned_before(clean, local, load_end, len(clean))
        region_end = len(clean) if rebound is None else rebound

        for use_pos in _deref_uses(clean, load_end, region_end, local):
            reaching = ctx.reaching_in(clean, load_end, use_pos)
            carried = False
            if not reaching:
                loop_end = _loop_scope_end(clean, use_pos, load_end=load_end)
                if loop_end is not None:
                    reaching = ctx.reaching_in(
                        clean, load_end, min(loop_end, region_end)
                    )
                    carried = bool(reaching)
            if not reaching:
                continue
            if ctx.call_is_type_pinned(func, clean, load_end, use_pos):
                continue
            line = _line_at(func, clean, use_pos)
            if line in seen or line in skip:
                break
            seen.add(line)
            twin = ctx.field_accessor_twin(accessor)
            order = (
                "a later iteration's use follows this iteration's call"
                if carried
                else "the call precedes the use"
            )
            # Error formatting reaches Python through %S/%R, but a dispatcher
            # is the more credible cause and the more useful thing to name.
            best = sorted(reaching, key=lambda n: n.startswith(("PyErr_", "PySys_")))
            findings.append(
                {
                    "type": "borrowed_field_deref_across_call",
                    "api_call": best[0],
                    "variable": local,
                    "line": line,
                    "load_line": _line_at(func, clean, load_end),
                    "source": f"{accessor}() -> {accessors[accessor]}",
                    "guarded_twin_line": twin,
                    "detail": (
                        f"'{local}' is a borrowed reference from {accessor}() "
                        f"(field {accessors[accessor]}) taken at line "
                        f"{_line_at(func, clean, load_end)} with no Py_INCREF; "
                        f"{', '.join(sorted(set(reaching)))} can run arbitrary "
                        f"Python before it is dereferenced here ({order}). "
                        f"Re-entrant code that clears the field drops the last "
                        f"reference and this dereference reads freed memory. "
                        + (
                            f"The same file already takes a strong reference on "
                            f"{accessor}()'s result at line {twin} -- that is the "
                            f"fix, applied here."
                            if twin
                            else f"No site in this file takes a strong reference "
                            f"on {accessor}()'s result, so confirm the field "
                            f"cannot be cleared re-entrantly before dismissing."
                        )
                    ),
                    "confidence": "high" if twin else "medium",
                }
            )
            break
    return findings


# ---------------------------------------------------------------------------
# Rules: new-reference balance
# ---------------------------------------------------------------------------


def _escaped_vars(clean: str) -> set[str]:
    """Variables handed to a call the scanner does not model.

    Once a pointer is passed to an arbitrary helper, ownership may have been
    transferred there and the balance analysis cannot say anything useful.
    """
    escaped: set[str] = set()
    for callee, args, _pos, _end in _iter_calls(clean):
        if _NO_ESCAPE_CALL_RE.match(callee) or callee in DECREF_APIS:
            continue
        # Note: a NEW_REF API is *not* exempt.  ``Py_BuildValue("N(N)", a, b)``
        # returns a new reference and consumes its arguments at the same time.
        escaped.update(_IDENT_RE.findall(args))
    # Anything appearing on the right-hand side of an assignment has been
    # stored somewhere the scanner cannot follow: an out-parameter
    # (``*p_result = result``), a context struct (``ctx->slots = new_slots``)
    # or simply another local (``listrepr = tmp``).
    for m in _ASSIGN_RHS_RE.finditer(clean):
        escaped.update(_IDENT_RE.findall(m.group(1)))
    return escaped


def analyze_function_refcounts(func: dict) -> list[dict]:
    """New-reference balance analysis for a single function."""
    clean = strip_comments_and_strings(func["body"])
    findings: list[dict] = []

    # Track new references acquired into a plain local.  Assignments through
    # ``*p =`` or ``x->m =`` transfer ownership out of the function and are
    # skipped by _ASSIGN_CALL_RE's lookbehind.
    locals_ = _declared_locals(func)
    new_refs: dict[str, dict] = {}
    for m in _ASSIGN_CALL_RE.finditer(clean):
        if not _is_local_assignment(clean, m.start()):
            continue
        if m.group(1) not in locals_:
            # A file-scope static / module global: CPython's module-init code
            # parks objects there for the life of the process on purpose.
            continue
        prefix = clean[: m.start()]
        new_refs[m.group(1)] = {
            "api": m.group(2),
            "pos": m.start(),
            "depth": prefix.count("{") - prefix.count("}"),
            "line": _line_at(func, clean, m.start()),
        }

    decreffed: set[str] = set()
    for m in _DECREF_RE.finditer(clean):
        decreffed.add(m.group(2))
    # Py_SETREF(dst, src) consumes src as well as replacing dst.
    for m in _SETREF_RE.finditer(clean):
        decreffed.add(m.group(2))

    increffed: set[str] = {m.group(2) for m in _INCREF_RE.finditer(clean)}

    stolen: set[str] = {m.group(2) for m in _STEAL_CALL_RE.finditer(clean)}
    returned: set[str] = {m.group(1) for m in _RETURN_RE.finditer(clean)}
    escaped = _escaped_vars(clean)

    gotos = [
        (
            m.start(),
            clean.count("{", 0, m.start()) - clean.count("}", 0, m.start()),
        )
        for m in _GOTO_ERROR_RE.finditer(clean)
    ]
    has_error_goto = bool(gotos)
    error_labels = set(_ERROR_LABEL_RE.findall(clean))

    error_cleanup_vars: set[str] = set()
    for label in error_labels:
        label_pattern = re.compile(
            re.escape(label) + r"\s*:(.+?)(?=\n\w+\s*:|$)",
            re.DOTALL,
        )
        m = label_pattern.search(clean)
        if m:
            for dm in _DECREF_RE.finditer(m.group(1)):
                error_cleanup_vars.add(dm.group(2))
            # A label that *returns* the variable is handing ownership to the
            # caller, not leaking it (`math_fsum`'s `_fsum_error: return sum`).
            for rm in _RETURN_RE.finditer(m.group(1)):
                error_cleanup_vars.add(rm.group(1))

    for var, info in new_refs.items():
        handled = (
            var in decreffed or var in stolen or var in increffed or var in returned
        )
        if not handled:
            if var in escaped:
                # Ownership may have moved into an unmodelled helper.
                continue
            findings.append(
                {
                    "type": "potential_leak",
                    "api_call": info["api"],
                    "variable": var,
                    "line": info["line"],
                    "detail": (
                        f"New reference from {info['api']} assigned to '{var}' "
                        f"is never DECREF'd, stolen, returned, or handed to "
                        f"another call"
                    ),
                    "confidence": "high",
                }
            )
            continue

        # Leak on an error path: returned on success, absent from cleanup.
        if (
            has_error_goto
            and var in returned
            and var not in error_cleanup_vars
            and var not in decreffed
            and var not in escaped
            # Flow gate: there must be a `goto` that is (a) after the
            # assignment, so the variable is live when it fires, and (b) not
            # inside the assignment's own NULL-check branch, where the
            # variable is provably NULL and cannot leak.
            and any(
                pos > info["pos"] and not _goto_guarded_by(clean, pos, var)
                for pos, _depth in gotos
            )
            # Scope gate: a variable declared inside a nested block is out of
            # scope at a function-level error label, so it cannot leak there.
            and info["depth"] == 0
        ):
            findings.append(
                {
                    "type": "potential_leak_on_error",
                    "api_call": info["api"],
                    "variable": var,
                    "line": info["line"],
                    "detail": (
                        f"New reference '{var}' from {info['api']} returned on "
                        f"success but not DECREF'd in error cleanup"
                    ),
                    "confidence": "medium",
                }
            )

    # Stolen then DECREF'd.  Only a double-free if the DECREF is genuinely
    # reached after the steal; the two commonly sit in mutually exclusive
    # branches, which is correct code.
    for m in _STEAL_CALL_RE.finditer(clean):
        var = m.group(2)
        if var not in new_refs:
            continue
        region_end = _same_block_region(clean, m.end())
        drop = None
        for d in _BARE_DECREF_RE.finditer(clean, m.end(), region_end):
            if d.group(2) != var:
                continue
            # The DECREF must sit at the same brace depth as the steal.  A
            # deeper one is the error branch of the steal's own failure test
            # (`if (PyModule_AddObject(...) < 0) { Py_DECREF(x); }`), where
            # nothing was stolen and the drop is correct.
            span = clean[m.end() : d.start()]
            if span.count("{") > span.count("}"):
                continue
            drop = d
            break
        if drop is None:
            continue
        findings.append(
            {
                "type": "potential_double_free",
                "api_call": m.group(1),
                "variable": var,
                "line": _line_at(func, clean, drop.start()),
                "detail": (
                    f"'{var}' is passed to the reference-stealing "
                    f"{m.group(1)} and then {drop.group(1)}'d in the same block "
                    f"— potential double-free"
                ),
                "confidence": "medium",
            }
        )

    return findings


# ---------------------------------------------------------------------------
# tp_init / tp_new safety
# ---------------------------------------------------------------------------

_INIT_ALLOC_APIS = with_private_aliases(
    frozenset(
        {
            "PyMem_Malloc",
            "PyMem_Calloc",
            "PyMem_Realloc",
            "malloc",
            "calloc",
            "realloc",
            "PyObject_New",
            "PyObject_GC_New",
            "PyList_New",
            "PyDict_New",
            "PyTuple_New",
            "PySet_New",
            "PyUnicode_FromString",
            "PyBytes_FromString",
            "Py_BuildValue",
            "PyObject_Call",
            "PyObject_CallFunction",
            "PyObject_CallMethod",
            "fopen",
            "open",
        }
    )
)

_INIT_ALLOC_RE = re.compile(
    r"\b("
    + "|".join(
        re.escape(api) for api in sorted(_INIT_ALLOC_APIS, key=len, reverse=True)
    )
    + r")\s*\("
)

# Argument Clinic emits `<Type>___init___impl` for `Type.__init__`. The name
# encodes the slot, so it is proof of tp_init-hood on its own.
_CLINIC_INIT_IMPL_RE = re.compile(r"^\w+___init___impl$")

_REINIT_GUARD_STATIC = [
    re.compile(r"already.?init", re.IGNORECASE),
    re.compile(r"cannot.?reinit", re.IGNORECASE),
    re.compile(r"\bPREVENT_INIT", re.IGNORECASE),
    re.compile(r"\binit_was_called\b"),
]


def _build_member_assign_re(param: str) -> re.Pattern:
    """Build regex matching assignment to ``param->member``."""
    return re.compile(rf"\b{re.escape(param)}\s*->\s*(\w+)\s*=(?!=)")


def _build_reinit_guards(param: str) -> list[re.Pattern]:
    """Build re-init guard patterns for a given parameter name."""
    return _REINIT_GUARD_STATIC + [
        re.compile(rf"\b{re.escape(param)}\s*->\s*initialized\b"),
        # Cleanup-before-assign: if (param->member != NULL) { Py_CLEAR/... }
        re.compile(
            rf"if\s*\(\s*{re.escape(param)}\s*->\s*\w+\s*!=\s*NULL\s*\)"
            r"[^}]*(?:Py_CLEAR|Py_XDECREF|Py_DECREF|PyMem_Free|free)\s*\("
        ),
        re.compile(rf"Py_X?SETREF\s*\(\s*{re.escape(param)}\s*->"),
        re.compile(rf"Py_CLEAR\s*\(\s*{re.escape(param)}\s*->"),
        # The member block comes from an initialiser helper that owns the
        # re-init decision itself (`stginfo = PyStgInfo_Init(st, self)`
        # raises "StgInfo ... is already initialized").  The scanner cannot
        # see guards implemented in a callee; this is the common spelling.
        re.compile(rf"{re.escape(param)}\s*=\s*\w*_Init\w*\s*\("),
        # Save-old-then-release: `old = self->f; self->f = new; Py_XDECREF(old);`
        # is `defaultdict.__init__`'s idiom and is re-init safe.
        re.compile(
            rf"([A-Za-z_]\w*)\s*=\s*{re.escape(param)}\s*->\s*\w+\s*;"
            r"(?:.|\n)*?Py_X?DECREF\s*\(\s*\1\s*\)"
        ),
    ]


_NON_ZEROING_ALLOC_RE = re.compile(r"\b(PyObject_New|PyObject_GC_New|malloc)\s*\(")

_ZEROING_ALLOC_PATTERN = r"\b(tp_alloc|PyType_GenericAlloc|calloc)\s*\("


def _build_zeroing_alloc_re(param: str) -> re.Pattern:
    """Build regex matching zeroing allocators including memset(param, 0)."""
    # `memset(&ob->structmembers, 0, ...)` zeroes just as `memset(ob, 0, ...)`
    # does; only the sub-object spelling differs.
    return re.compile(
        _ZEROING_ALLOC_PATTERN + r"|" + rf"\bmemset\s*\(\s*&?\s*{re.escape(param)}\b"
    )


def _extract_self_param(func: dict) -> str | None:
    """Name of the instance pointer: the most frequently dereferenced local."""
    body = func.get("body", "")
    deref_re = re.compile(r"\b(\w+)\s*->\s*\w+")
    candidates: dict[str, int] = {}
    for m in deref_re.finditer(body):
        p = m.group(1)
        if p not in ("NULL", "type", "PyExc", "Py_TYPE", "ob_type"):
            candidates[p] = candidates.get(p, 0) + 1
    if candidates:
        return max(candidates, key=lambda k: candidates[k])
    return None


def _is_tp_init(func: dict, slots: dict[str, set[str]]) -> bool:
    """Is ``func`` the body of a Python-level ``__init__``?

    Two independent proofs, because slot registration alone missed every
    instance in CPython:

    * real ``tp_init`` registration (all three slot-table spellings), and
    * the Argument Clinic name, which *encodes* the slot.  Clinic emits
      ``<Type>___init___impl`` for ``Type.__init__``; the registered slot
      function is the generated ``<Type>___init__`` in ``clinic/*.c.h``, and
      real code often puts a further hand-written wrapper in between
      (``Modules/_struct.c`` registers ``s_init``, which calls
      ``Struct___init__``, which calls ``Struct___init___impl``).  Requiring
      registration of the *impl* therefore resolved nothing: the rule fired
      zero times over ``Objects/``, ``Modules/`` and ``Python/``.
    """
    return bool(
        _registered_as(func, slots, "tp_init")
        or _CLINIC_INIT_IMPL_RE.match(func["name"])
    )


_CALL_HEAD_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


def _close_paren(text: str, open_idx: int) -> int | None:
    """Index of the ``)`` matching the ``(`` at ``open_idx``."""
    depth = 0
    for k in range(open_idx, min(len(text), open_idx + 4000)):
        if text[k] == "(":
            depth += 1
        elif text[k] == ")":
            depth -= 1
            if depth == 0:
                return k
    return None


def _split_top_level(text: str) -> list[str]:
    """Split on commas at paren/bracket depth 0."""
    out: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(text[start:i])
            start = i + 1
    out.append(text[start:])
    return out


def _argument_index(args_text: str, name: str) -> int | None:
    """Position at which ``name`` is passed, ignoring casts and ``&``."""
    for i, arg in enumerate(_split_top_level(args_text)):
        expr = re.sub(r"^\s*(?:\(\s*[\w\s*]+\)\s*)*&?\s*", "", arg).strip()
        if expr == name:
            return i
    return None


def _param_name_at(params_text: str, index: int) -> str | None:
    """Name of the declared parameter at ``index`` in a signature."""
    parts = _split_top_level(params_text)
    if index >= len(parts):
        return None
    m = re.search(r"([A-Za-z_]\w*)\s*(?:\[\s*\])?\s*$", parts[index].strip())
    return m.group(1) if m else None


def _init_chain(
    func: dict,
    param: str,
    functions: list[dict],
) -> list[tuple[dict, str]]:
    """``(function, receiver-name)`` pairs reachable from an init, ≤2 hops.

    CPython routinely splits the real mutation two calls away from the slot
    body: ``Struct___init___impl`` -> ``set_format(self, ...)`` ->
    ``prepare_s(self, ...)``, and only ``prepare_s`` frees and replaces
    ``self->s_codes``.  Only file-local callees that are handed the receiver
    are followed, and the receiver is re-derived in the callee's own scope.
    """
    by_name = {f["name"]: f for f in functions}
    chain = [(func, param)]
    seen = {func["name"]}
    frontier = [(func, param)]
    for _ in range(2):
        nxt: list[tuple[dict, str]] = []
        for caller, recv in frontier:
            body = strip_comments_and_strings(caller["body"])
            for m in _CALL_HEAD_RE.finditer(body):
                callee = by_name.get(m.group(1))
                if callee is None or callee["name"] in seen:
                    continue
                close = _close_paren(body, m.end() - 1)
                if close is None:
                    continue
                idx = _argument_index(body[m.end() : close], recv)
                if idx is None:
                    continue
                seen.add(callee["name"])
                # The callee's own name for the receiver is its parameter at
                # the position the receiver was passed in — `set_format` never
                # dereferences its `self`, so deriving the name from the body
                # would drop the hop entirely.
                callee_recv = _param_name_at(callee.get("params", ""), idx)
                if callee_recv is None:
                    callee_recv = _extract_self_param(callee)
                if callee_recv is None:
                    continue
                nxt.append((callee, callee_recv))
                chain.append((callee, callee_recv))
        frontier = nxt
    return chain


def _replaced_and_assigned(
    chain: list[tuple[dict, str]],
) -> tuple[set[str], set[str]]:
    """Members the init chain *destroys and replaces*, and all it assigns."""
    replaced: set[str] = set()
    assigned: set[str] = set()
    for fn, recv in chain:
        clean = strip_comments_and_strings(fn["body"])
        assigned.update(_build_member_assign_re(recv).findall(clean))
        r = re.escape(recv)
        for pat in (
            (
                rf"\b(?:PyMem_Free|PyMem_RawFree|PyObject_Free|free)\s*\("
                rf"\s*{r}\s*->\s*(\w+)"
            ),
            rf"\bPy_(?:CLEAR|X?DECREF)\s*\(\s*{r}\s*->\s*(\w+)",
            rf"\bPy_X?SETREF\s*\(\s*{r}\s*->\s*(\w+)",
        ):
            replaced.update(re.findall(pat, clean))
    # A member is only *replaced* if the same body also assigns it; a bare
    # release with no re-assignment is a destructor, not a re-init.
    return replaced & assigned, assigned


def _two_level_readers(
    chain_names: set[str],
    members: set[str],
    functions: list[dict],
) -> list[dict]:
    """Functions reading ``x->owner->member`` — the outstanding-view signature.

    A one-level ``self->member`` read is the owner's own method and sees the
    new state, which is correct.  A *two*-level read means some other object
    stored a pointer to the owner and is still reading through it: an
    iterator, a view, a cached-state consumer.
    """
    out: list[dict] = []
    for fn in functions:
        if fn["name"] in chain_names:
            continue
        clean = strip_comments_and_strings(fn["body"])
        for m in re.finditer(r"\b(\w+)\s*->\s*(\w+)\s*->\s*(\w+)\b", clean):
            if m.group(3) not in members:
                continue
            out.append(
                {
                    "function": fn["name"],
                    "line": _line_at(fn, clean, m.start()),
                    "expression": f"{m.group(1)}->{m.group(2)}->{m.group(3)}",
                    "member": m.group(3),
                }
            )
    return out


# Reader functions whose name marks them as an iterator/view step: a stale read
# there is reached by ordinary iteration, not by a contrived call order.
_VIEW_READER_RE = re.compile(
    r"iternext|_next$|_len$|length_hint|_iter$|getitem|subscript|_size$",
    re.IGNORECASE,
)


def check_init_reinit_safety(
    func: dict,
    slots: dict[str, set[str]],
    functions: list[dict] | None = None,
) -> list[dict]:
    """A re-callable ``tp_init`` that mutates state an outstanding view reads.

    The hazard is **not** the leak the rule was originally written for.
    ``__init__`` is an ordinary method: Python code may call it again on a live
    object.  When the second call frees and replaces state that another object
    already captured a pointer to, every invariant that other object validated
    at construction time is silently void.

    ``Modules/_struct.c`` is the exemplar and is live at 3.16.0a0::

        s = struct.Struct("i")
        it = s.iter_unpack(b"\\0" * 8)   # validates 8 %% s_size == 0
        next(it)
        s.__init__("100i")               # prepare_s frees s_codes, resets s_size
        next(it)                         # reads 400 bytes out of an 8-byte buffer

    ``unpackiter_iternext`` bounds itself with ``assert(self->index +
    self->so->s_size <= self->buf.len)`` — an assert, so a release build just
    reads past the end; ``unpackiter_len`` divides by ``self->so->s_size`` and
    takes SIGFPE when it becomes 0.

    Note the polarity flip this cost: ``Py_XSETREF(self->m, ...)`` /
    ``Py_CLEAR(self->m)`` / ``if (self->m) PyMem_Free(self->m)`` used to
    *suppress* the finding as evidence of re-init safety.  Under the real
    hazard they are the opposite — they are the proof that the second call
    destroys what the first one published.
    """
    if not _is_tp_init(func, slots):
        return []
    # For a tp_init the receiver is the first parameter by contract, so fall
    # back to it: an init whose body only *forwards* the receiver to a helper
    # (`return set_format(self, fmt);`) dereferences nothing of its own.
    param = _extract_self_param(func) or _param_name_at(func.get("params", ""), 0)
    if param is None:
        return []

    # A guard that *rejects* the second call is a genuine exemption. Checked
    # against the raw body: the "already initialized" sentinel lives in a
    # string literal that strip_comments_and_strings() blanks out.
    for pattern in _REINIT_GUARD_STATIC + [
        re.compile(rf"\b{re.escape(param)}\s*->\s*initialized\b"),
        re.compile(rf"{re.escape(param)}\s*=\s*\w*_Init\w*\s*\("),
    ]:
        if pattern.search(func["body"]):
            return []

    chain = _init_chain(func, param, functions or [func])
    replaced, assigned = _replaced_and_assigned(chain)
    if not replaced:
        return []

    readers = _two_level_readers(
        {fn["name"] for fn, _ in chain},
        assigned,
        functions or [],
    )
    if not readers:
        return []

    stale = sorted({r["member"] for r in readers})
    view_readers = [r for r in readers if _VIEW_READER_RE.search(r["function"])]
    cited = view_readers or readers
    return [
        {
            "type": "init_not_reinit_safe",
            "line": func["start_line"],
            "replaced_members": sorted(replaced),
            "stale_members": stale,
            "readers": cited[:8],
            "detail": (
                f"__init__ '{func['name']}' is re-callable on a live object and "
                f"its second call destroys and replaces {param}->"
                f"{f', {param}->'.join(sorted(replaced))}. "
                f"{cited[0]['function']} (line {cited[0]['line']}) still reads "
                f"that state through a stored owner pointer "
                f"({cited[0]['expression']}), so every invariant it validated at "
                f"construction time — buffer bounds, element size, cached counts "
                f"— is void after the re-init. Fix: reject the second call, or "
                f"give the view its own snapshot of "
                f"{', '.join(stale)}"
            ),
            "confidence": "high" if view_readers else "medium",
        }
    ]


def check_new_member_init(
    func: dict,
    slots: dict[str, set[str]],
) -> list[dict]:
    """A registered ``tp_new`` that can leave members uninitialised.

    Two gates the original rule lacked, both required to make this rule mean
    anything on CPython's own code:

    * real ``tp_new`` registration -- ``PyCell_New`` and friends are C-API
      constructors, not slots, and ``object.__new__`` never routes through a
      custom ``tp_new`` anyway (it allocates via ``tp_alloc``, which zeroes,
      and refuses outright when ``tp_new`` is overridden);
    * a *fallible call* between the allocation and the member stores -- without
      one there is no window in which a half-built object escapes to
      ``tp_dealloc``.  Storing a real value (``Py_NewRef(x)``) into every
      member counts as initialisation just as much as ``= NULL`` does.
    """
    if not _registered_as(func, slots, "tp_new"):
        return []

    clean = strip_comments_and_strings(func["body"])
    param = _extract_self_param(func) or "self"

    if _build_zeroing_alloc_re(param).search(clean):
        return []

    non_zero_m = _NON_ZEROING_ALLOC_RE.search(clean)
    if not non_zero_m:
        return []

    # Any member store counts as initialisation, not just `= NULL`.
    member_re = _build_member_assign_re(param)
    stores = list(member_re.finditer(clean, non_zero_m.end()))
    first_store = stores[0].start() if stores else len(clean)

    # No window between the allocation and the stores => nothing can observe
    # the uninitialised members.  A bare Py_DECREF on the allocator's own
    # NULL-check branch does not count: that path returns.
    window = [
        name
        for name in python_reaching_calls(clean, non_zero_m.end(), first_store)
        if name not in _REFCOUNT_ONLY_REACHING
    ]
    if not window:
        return []

    return [
        {
            "type": "new_missing_member_init",
            "line": _line_at(func, clean, non_zero_m.start()),
            "detail": (
                f"tp_new '{func['name']}' allocates with the non-zeroing "
                f"{non_zero_m.group(1)}() and then makes a fallible, "
                f"Python-reaching call before initialising {param}'s pointer "
                f"members — an early error path hands a half-built object to "
                f"tp_dealloc"
            ),
            "confidence": "medium",
        }
    ]


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Analyze refcount patterns for the given target path."""
    target_path = Path(target).resolve()
    project_root = find_cpython_root(target_path)
    if project_root is None:
        # project_root must be a directory — it is what findings are relativized to.
        project_root = target_path if target_path.is_dir() else target_path.parent
    # scan_root is the target itself: discover_c_files() yields just that file for a
    # file root. Using .parent here silently widened a single-file scan to the whole
    # directory (scan_refcounts.py Objects/iterobject.c scanned all 123 files).
    scan_root = target_path

    all_findings: list[dict] = []
    functions_analyzed = 0
    files_analyzed = 0

    for filepath in discover_c_files(scan_root, max_files=max_files):
        files_analyzed += 1
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        try:
            rel = str(filepath.relative_to(project_root))
        except ValueError:
            rel = str(filepath)
        # Slot registration must be read from the raw source: the positional
        # PyTypeObject form hides the slot name in a comment.
        slots = collect_slot_registrations(source)

        functions = find_functions(source)
        ctx = FileRefContext(source, functions)
        accessors = ctx.field_accessors(source)

        for func in functions:
            functions_analyzed += 1
            func_findings = analyze_function_refcounts(func)
            stale = check_stale_slot_decref(func)
            func_findings.extend(stale)
            func_findings.extend(check_owner_freed_before_use(func))
            borrowed = check_borrowed_ref_across_call(
                func,
                skip_lines={f["line"] for f in stale},
            )
            func_findings.extend(borrowed)
            func_findings.extend(check_slot_transfer_across_call(func, ctx))
            slot_use = check_stale_slot_use(
                func,
                ctx,
                skip_lines={f["line"] for f in stale} | {f["line"] for f in borrowed},
            )
            func_findings.extend(slot_use)
            func_findings.extend(
                check_borrowed_field_deref(
                    func,
                    ctx,
                    accessors=accessors,
                    skip_lines={f["line"] for f in stale}
                    | {f["line"] for f in borrowed}
                    | {f["line"] for f in slot_use},
                )
            )
            func_findings.extend(check_init_reinit_safety(func, slots, functions))
            func_findings.extend(check_new_member_init(func, slots))
            for finding in func_findings:
                finding["file"] = rel
                finding["function"] = func["name"]
                all_findings.append(finding)

    def count(*types: str) -> int:
        return len([f for f in all_findings if f["type"] in types])

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_analyzed": files_analyzed,
        "functions_analyzed": functions_analyzed,
        "findings": all_findings,
        "summary": {
            "potential_leaks": count("potential_leak", "potential_leak_on_error"),
            "potential_double_frees": count("potential_double_free"),
            "stale_slot_decref": count("stale_slot_decref"),
            "owner_freed_before_use": count("owner_freed_before_use"),
            "borrowed_ref_across_call": count("borrowed_ref_across_call"),
            "slot_transfer_across_call": count("slot_transfer_across_call"),
            "stale_slot_use": count("stale_slot_use"),
            "borrowed_field_deref_across_call": count(
                "borrowed_field_deref_across_call"
            ),
            "init_not_reinit_safe": count("init_not_reinit_safe"),
            "new_missing_member_init": count("new_missing_member_init"),
            "total_findings": len(all_findings),
            "high_confidence": len(
                [f for f in all_findings if f.get("confidence") == "high"]
            ),
            "medium_confidence": len(
                [f for f in all_findings if f.get("confidence") == "medium"]
            ),
            "low_confidence": len(
                [f for f in all_findings if f.get("confidence") == "low"]
            ),
        },
    }


def main() -> None:
    """CLI entry point: emit the analysis as JSON on stdout."""
    max_files = 0
    positional: list[str] = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--max-files" and i + 1 < len(argv):
            max_files = int(argv[i + 1])
            i += 2
        elif argv[i].startswith("--"):
            i += 1
        else:
            positional.append(argv[i])
            i += 1
    target = positional[0] if positional else "."
    result = analyze(target, max_files=max_files)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

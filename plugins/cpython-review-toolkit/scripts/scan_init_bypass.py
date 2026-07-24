#!/usr/bin/env python3
"""Scan CPython C source for __init__-bypass NULL dereferences.

The dangerous pattern: a C method/slot reads ``self->field`` and INCREFs it,
calls it, or dereferences it with **no NULL check**, but the field can
legitimately be NULL because construction can bypass the ``tp_init`` that would
have set it. There are three ways the field becomes NULL:

1. **``__new__`` / subclass bypass.** The type wires a ``tp_init`` that assigns
   ``self->field`` but has no real ``tp_new`` (its ``tp_new`` is ``0`` or
   ``PyType_GenericNew``, and there is no ``DISALLOW_INSTANTIATION``), so
   ``T.__new__(T)`` produces a zeroed object on which ``field`` is NULL and
   ``__init__`` never ran.
2. **A deletable member.** The field is exposed via a ``PyMemberDef`` with
   a deletable object type (``T_OBJECT`` / ``T_OBJECT_EX`` and the ``Py_``/``_Py_``
   spellings, without ``READONLY``), so ``del obj.field`` leaves it NULL.
3. **A deletable getset.** The field is exposed via a ``PyGetSetDef`` whose
   setter accepts ``value == NULL`` (i.e. does not reject deletion) and stores
   into the field, so ``del obj.attr`` leaves it NULL.

A guard like ``if (self->field != Py_None)`` or ``if (!Py_IsNone(self->field))``
is **not** a NULL guard: after the bypass the field is NULL, ``NULL != Py_None``
is true, and control enters the crashing block.

Slot-table forms understood (all three — see the ``form``-specificity note):
  - ``PyType_Spec``:   ``{Py_tp_init, foo_init}``
  - designated:        ``.tp_init = foo_init``
  - **positional**:    ``foo_init,   /* tp_init */`` — the dominant form in
    ``Objects/`` and ``Python/``. Its *only* identifying token is the trailing
    slot comment, so the positional regexes must run against the **raw** source;
    ``strip_comments()`` erases exactly the marker they key on.

Confirmed exemplars this targets:
  - gh-152954 — ``sqlite3.Connection.__new__`` bypass leaves ``row_factory`` NULL,
    then ``Py_INCREF(self->row_factory)`` crashes (Modules/_sqlite/connection.c).
  - gh-152817 — ``del cursor.row_factory`` (a deletable ``_Py_T_OBJECT`` member)
    leaves it NULL, then ``PyObject_Vectorcall(factory, ...)`` crashes
    (Modules/_sqlite/cursor.c).
  - gh-144330 — ``classmethod``/``staticmethod`` had ``tp_init`` +
    ``PyType_GenericNew``; fixed by moving initialization into a real ``tp_new``.
  - ``bytearray`` (main, 3.16.0a0) — ``tp_init`` + ``PyType_GenericNew`` leaves
    ``ob_bytes_object`` NULL; ``_PyBytes_Resize(&obj->ob_bytes_object, ...)``
    dereferences ``*pv`` unguarded → SIGSEGV. This is the ``addr_deref`` sink.

The scanner is deliberately high-recall: it flags candidates, an agent confirms
the field can really be NULL and that the deref is genuinely unguarded. It
cannot see cross-function guarantees (a caller that always sets the field).

**Canary:** ``total_nullable_fields`` in the envelope reports how many fields the
scanner even considered nullable. Zero findings with zero nullable fields means
the rule never fired on this corpus — not that the corpus is clean. Always check
``total_nullable_fields`` before reporting a clean negative.

Usage:
    python scan_init_bypass.py [path] [--max-files N]
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
    parse_bytes,
    strip_comments,
)

# Member-type constants whose members are *deletable* (``del obj.attr`` sets the
# slot to NULL). Ordered longest-first so regex alternation prefers the ``_EX``
# spellings over their prefixes. READONLY members are excluded at match time.
_DELETABLE_MEMBER_TYPES = (
    "_Py_T_OBJECT_EX",
    "Py_T_OBJECT_EX",
    "T_OBJECT_EX",
    "_Py_T_OBJECT",
    "Py_T_OBJECT",
    "T_OBJECT",
)

# A PyMemberDef entry: {"name", TYPE, offsetof(Struct, field), flags[, "doc"]}.
# We capture the *struct field* (from offsetof — that's what ``self->field``
# reads) and the flags text so READONLY members can be filtered. The offset is
# often wrapped in a file-local UPPERCASE macro (``#define OFF(x) offsetof(...)``
# in funcobject.c / methodobject.c), so accept that spelling too.
_MEMBER_ENTRY_RE = re.compile(
    r'\{\s*"[^"]*"\s*,\s*'
    r"(" + "|".join(_DELETABLE_MEMBER_TYPES) + r")\s*,\s*"
    r"(?:offsetof\s*\(\s*\w+\s*,\s*(\w+)\s*\)|[A-Z_]\w*\s*\(\s*(\w+)\s*\))\s*,\s*"
    r"([^}]*)\}"
)

# A PyGetSetDef entry: {"name", (getter)get_fn, (setter)set_fn[, doc[, closure]]}.
# We capture the setter; a setter that accepts ``value == NULL`` implements
# ``del obj.attr`` and can therefore leave its backing field NULL.
_GETSET_ENTRY_RE = re.compile(
    r'\{\s*"[^"]*"\s*,\s*'
    r"(?:\(\s*getter\s*\)\s*)?(?:&\s*)?(?:\w+)\s*,\s*"
    r"(?:\(\s*setter\s*\)\s*)?(?:&\s*)?(\w+)\s*[,}]"
)
# Messages a setter emits when it *rejects* deletion — the discriminator that
# removes the ``del`` path.
_DELETE_REJECT_MSGS = (
    "cannot delete",
    "can't delete",
    "cannot be deleted",
    "may not be deleted",
    "not deletable",
    "readonly attribute",
    "read-only attribute",
)
# Refcount helpers a NULL-accepting setter legitimately passes the value to on
# its way into the field. Anything *else* taking the value is opaque validation
# we must assume rejects deletion.
_NULL_TRANSPARENT_HELPERS = frozenset(
    {
        "Py_NewRef",
        "Py_XNewRef",
        "Py_INCREF",
        "Py_XINCREF",
        "Py_DECREF",
        "Py_XDECREF",
        "Py_CLEAR",
        "Py_SETREF",
        "Py_XSETREF",
        "_Py_NewRef",
        "_Py_XNewRef",
    }
)

# tp_init wiring (PyType_Slot form and static-PyTypeObject designated form).
_TP_INIT_SLOT_RE = re.compile(r"\bPy_tp_init\s*,\s*(?:&\s*)?(\w+)")
_TP_INIT_DESIG_RE = re.compile(
    r"\.tp_init\s*=\s*(?:\(\s*[\w\s\*]+\)\s*)?(?:&\s*)?(\w+)"
)
# Any of these tokens means the type controls instantiation itself (a real
# tp_new, or an explicit disallow) — the __new__-bypass reasoning does not apply,
# so we conservatively disable the *spec/designated* bypass signal for the whole
# file. The positional form below does per-type-block pairing instead and is not
# subject to this whole-file kill switch.
_TP_NEW_TOKENS_RE = re.compile(r"\bPy_tp_new\b|\.tp_new\s*=|\bDISALLOW_INSTANTIATION\b")

# Positional static-``PyTypeObject`` slot table — the dominant form in Objects/
# and Python/. The trailing slot comment is the *only* marker, so these must be
# matched against the RAW source (strip_comments() deletes them).
_TP_INIT_POS_RE = re.compile(r"^[ \t]*(\w+)\s*,\s*/\*\s*tp_init\s*\*/", re.MULTILINE)
_TP_NEW_POS_RE = re.compile(r"^[ \t]*(\w+)\s*,\s*/\*\s*tp_new\s*\*/", re.MULTILINE)
_TP_NEW_DESIG_VAL_RE = re.compile(
    r"\.tp_new\s*=\s*(?:\(\s*[\w\s\*]+\)\s*)?(?:&\s*)?(\w+)"
)
# End of a file-scope initializer block (``};`` in column 0).
_BLOCK_END_RE = re.compile(r"^\};", re.MULTILINE)
# A tp_new value that does NOT protect against the bypass: 0/NULL means the slot
# is inherited (ultimately object.__new__), and PyType_GenericNew allocates a
# zeroed instance without running tp_init. Either way ``T.__new__(T)`` works.
_INHERITED_NEW = frozenset({"0", "NULL", "PyType_GenericNew"})

# Sinks that crash on a NULL argument.
# Single-argument refcount ops (Py_XINCREF / Py_IncRef are NULL-safe — excluded).
_INCREF_SINKS = frozenset({"Py_INCREF", "Py_NewRef", "_Py_NewRef"})
# Call family — the first argument (the callable / receiver object) must be
# non-NULL.
_CALL_SINKS = frozenset(
    {
        "PyObject_Call",
        "PyObject_CallObject",
        "PyObject_CallNoArgs",
        "PyObject_CallOneArg",
        "PyObject_CallFunction",
        "PyObject_CallFunctionObjArgs",
        "PyObject_CallMethod",
        "PyObject_CallMethodObjArgs",
        "PyObject_CallMethodOneArg",
        "PyObject_CallMethodNoArgs",
        "PyObject_Vectorcall",
        "PyObject_VectorcallDict",
        "PyObject_VectorcallMethod",
        "_PyObject_Vectorcall",
        "_PyObject_CallNoArgs",
        "_PyObject_FastCall",
        "_PyObject_FastCallDict",
    }
)
# Dereference macros — the single argument is dereferenced immediately.
_DEREF_SINKS = frozenset(
    {"Py_TYPE", "Py_SIZE", "Py_REFCNT", "Py_SET_TYPE", "Py_SET_SIZE"}
)
# ``PyObject **``-taking APIs that dereference ``*pv`` with NO NULL check, so
# ``f(&self->field, ...)`` crashes when the field is NULL. Deliberately tiny and
# evidence-based: the rest of the family DOES check — _PyTuple_Resize has
# ``v == NULL ||`` (Objects/tupleobject.c), PyBytes_Concat has
# ``if (*pv == NULL) return`` and PyBytes_ConcatAndDel likewise
# (Objects/bytesobject.c). _PyBytes_Resize is the only unguarded member; it does
# ``v = *pv;`` then ``Py_TYPE(v)`` via PyBytes_Check.
_ADDR_DEREF_SINKS = frozenset({"_PyBytes_Resize"})

# RHS ends in ``-> field`` (possibly through casts), e.g. ``self->row_factory``
# or ``((FooObject *)self)->row_factory``.
_TRAILING_FIELD_RE = re.compile(r"->\s*(\w+)\s*$")
# The base identifier of a ``base->field`` read (after any casts / address-of).
_FIELD_BASE_RE = re.compile(r"(\w+)\s*\)*\s*->\s*\w+\s*$")
# A function call token (``foo(``) — used to reject call results as field aliases.
_CALL_TOKEN_RE = re.compile(r"[A-Za-z_]\w*\s*\(")


def _receiver_name(func: dict) -> str | None:
    """Return the identifier of the first parameter (the ``self`` receiver)."""
    params = func["parameters"].strip()
    if not params:
        return None
    first = params.split(",")[0]
    idents = re.findall(r"[A-Za-z_]\w*", first)
    return idents[-1] if idents else None


def _non_receiver_params(func: dict) -> set[str]:
    """Return the names of every parameter *after* the receiver.

    Field names collide across struct types in large files: ``superobject`` has
    an ``obj`` field and so does ``Py_buffer``, so ``Py_TYPE(buffer->obj)`` in
    ``releasebuffer_call_python`` (Objects/typeobject.c) reads as a ``super``
    field to a name-keyed scanner. A read based on a *later* parameter is never
    the receiver's field, so excluding those kills that collision class without
    costing recall — slot functions always take ``self`` first.
    """
    params = [p.strip() for p in func["parameters"].split(",")][1:]
    names: set[str] = set()
    for p in params:
        idents = re.findall(r"[A-Za-z_]\w*", p)
        if idents:
            names.add(idents[-1])
    return names


def _receiver_aliases(body: str, recv: str) -> set[str]:
    """Return ``recv`` plus the locals it is downcast into.

    A tp_init whose signature takes ``PyObject *self`` almost always re-casts it
    before touching fields — ``classmethod *cm = (classmethod *)self;`` or
    ``ElementObject *e = _Element_CAST(self);``. Without this, receiver-anchored
    field collection silently returns nothing, which is why the shipped scanner
    found no fields in the pre-gh-144330 ``cm_init``/``sm_init`` even once their
    slot tables were parsed.
    """
    esc = re.escape(recv)
    names = {recv}
    # local = (Type *)recv;
    for m in re.finditer(rf"\b(\w+)\s*=\s*\(\s*[\w\s*]+\)\s*{esc}\s*;", body):
        names.add(m.group(1))
    # local = _Foo_CAST(recv);
    for m in re.finditer(rf"\b(\w+)\s*=\s*(\w*_CAST)\s*\(\s*{esc}\s*\)\s*;", body):
        names.add(m.group(1))
    return names


def _fields_set_in_init(func: dict, recv: str) -> set[str]:
    """Return struct fields the init function assigns via ``recv->field``."""
    body = strip_comments(func["body"])
    fields: set[str] = set()
    for name in _receiver_aliases(body, recv):
        esc = re.escape(name)
        # Direct assignment: recv->field = ...  (but not ==, >=, <=, !=).
        for m in re.finditer(rf"\b{esc}\s*->\s*(\w+)\s*=(?!=)", body):
            fields.add(m.group(1))
        # Py_SETREF / Py_XSETREF(recv->field, ...).
        for m in re.finditer(rf"Py_X?SETREF\s*\(\s*{esc}\s*->\s*(\w+)", body):
            fields.add(m.group(1))
    # Inline cast at the store site: ((Type *)recv)->field = ...
    esc = re.escape(recv)
    for m in re.finditer(rf"\)\s*{esc}\s*\)\s*->\s*(\w+)\s*=(?!=)", body):
        fields.add(m.group(1))
    return fields


def _fields_assigned_anywhere(body: str) -> set[str]:
    """Return every ``x->field`` a body stores into, whatever the receiver.

    Setters routinely re-cast ``self`` into a local (``op = _PyCell_CAST(self)``)
    before storing, so receiver-anchored matching misses them. Setter bodies are
    small, which keeps this loose form safe here.
    """
    fields: set[str] = set()
    for m in re.finditer(r"\b\w+\s*->\s*(\w+)\s*=(?!=)", body):
        fields.add(m.group(1))
    for m in re.finditer(r"Py_X?SETREF\s*\(\s*\w+\s*->\s*(\w+)", body):
        fields.add(m.group(1))
    return fields


def _positional_bypassable_inits(source: str) -> list[str]:
    """Return tp_init values from positional slot tables that lack a real tp_new.

    Runs against the **raw** source: the positional form's only marker is the
    ``/* tp_init */`` trailing comment, which ``strip_comments()`` deletes.

    Each ``tp_init`` is paired with the ``tp_new`` of the *same* ``PyTypeObject``
    block — the search window ends at whichever comes first: the next positional
    ``tp_init`` marker or the end of the enclosing initializer. That per-block
    pairing replaces the whole-file ``_TP_NEW_TOKENS_RE`` kill switch, which is
    far too coarse for a file holding several types (``descrobject.c`` defines
    ``mappingproxy_new`` and would otherwise hide ``property``).
    """
    init_matches = list(_TP_INIT_POS_RE.finditer(source))
    result: list[str] = []
    for i, m in enumerate(init_matches):
        init_value = m.group(1)
        if init_value in ("0", "NULL"):
            continue
        window_end = len(source)
        if i + 1 < len(init_matches):
            window_end = init_matches[i + 1].start()
        block_end = _BLOCK_END_RE.search(source, m.end())
        if block_end is not None:
            window_end = min(window_end, block_end.start())
        window = source[m.end() : window_end]
        if "DISALLOW_INSTANTIATION" in window:
            continue
        candidates = [
            (n.start(), n.group(1))
            for n in (
                _TP_NEW_POS_RE.search(window),
                _TP_NEW_DESIG_VAL_RE.search(window),
            )
            if n is not None
        ]
        if candidates:
            new_value = min(candidates)[1]
            if new_value not in _INHERITED_NEW:
                continue
        # No tp_new in this block at all: the slot is inherited, which is the
        # bypassable case.
        result.append(init_value)
    return result


def _nullable_getset_fields(clean: str, functions: list[dict]) -> set[str]:
    """Return fields written by a ``PyGetSetDef`` setter that accepts deletion.

    ``del obj.attr`` calls the setter with ``value == NULL``. A setter that does
    not reject that and stores into a struct field leaves the field NULL — the
    gh-152817 mechanism generalized past fields that happen to also carry a
    ``PyMemberDef`` entry.
    """
    setter_names = {m.group(1) for m in _GETSET_ENTRY_RE.finditer(clean)} - {
        "NULL",
        "0",
    }
    if not setter_names:
        return set()
    fields: set[str] = set()
    for func in functions:
        if func["name"] not in setter_names:
            continue
        body = strip_comments(func["body"])
        if _rejects_deletion(func, body):
            continue
        fields |= _fields_assigned_anywhere(body)
    return fields


def _callees_taking(body: str, ident: str) -> set[str]:
    """Return the names of calls/macros in ``body`` that receive ``ident``."""
    names: set[str] = set()
    word = re.compile(rf"\b{re.escape(ident)}\b")
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", body):
        depth = 0
        start = m.end()
        for i in range(m.end() - 1, len(body)):
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
                if depth == 0:
                    if word.search(body[start:i]):
                        names.add(m.group(1))
                    break
    return names


def _rejects_deletion(func: dict, body: str) -> bool:
    """True if a getset setter refuses ``del obj.attr`` (a NULL value).

    Three signals, any of which means the ``del`` path is closed. The third is
    the important one: CPython overwhelmingly factors the rejection into a
    helper, so a setter whose body has no visible NULL check is usually still
    protected — ``element_tag_setter`` calls the ``_VALIDATE_ATTR_VALUE(value)``
    macro (Modules/_elementtree.c) and ``type_set_qualname`` calls
    ``check_set_special_type_attr(type, value, ...)`` (Objects/typeobject.c).
    Both raise "can't delete ...". Treating unresolved indirection over the
    value parameter as a rejection keeps this source high-precision at the cost
    of some recall.
    """
    lowered = body.lower()
    if any(msg in lowered for msg in _DELETE_REJECT_MSGS):
        return True
    params = [p.strip() for p in func["parameters"].split(",")]
    if len(params) < 2:
        return False
    idents = re.findall(r"[A-Za-z_]\w*", params[1])
    if not idents:
        return False
    val_name = idents[-1]
    val = re.escape(val_name)
    # ``if (value == NULL) { ... return -1; }`` — an early-out that rejects
    # deletion. The window is deliberately short so a NULL check that *handles*
    # deletion (and then stores NULL into the field) is not mistaken for one.
    guard = re.compile(rf"\b{val}\s*==\s*NULL\b|!\s*\b{val}\b(?!\s*(?:=|->|\.))")
    for m in guard.finditer(body):
        window = body[m.end() : m.end() + 200]
        if re.search(r"return\s+-1|goto\b", window):
            return True
    # The value is handed to something we cannot see through.
    return bool(_callees_taking(body, val_name) - _NULL_TRANSPARENT_HELPERS)


def _collect_nullable_fields(source: str, functions: list[dict]) -> dict[str, set[str]]:
    """Map ``field_name -> {reasons}`` for fields that can legitimately be NULL.

    Reasons: ``deletable_member`` (a deletable PyMemberDef entry),
    ``deletable_getset`` (a PyGetSetDef setter that accepts a NULL value), and
    ``new_bypass`` (assigned in a tp_init that no real tp_new protects).
    """
    clean = strip_comments(source)
    nullable: dict[str, set[str]] = defaultdict(set)

    # (1) Deletable members.
    for m in _MEMBER_ENTRY_RE.finditer(clean):
        field, flags = (m.group(2) or m.group(3)), m.group(4)
        if "READONLY" in flags:
            continue
        nullable[field].add("deletable_member")

    # (2) Deletable getsets — a setter that accepts ``value == NULL``.
    for field in _nullable_getset_fields(clean, functions):
        nullable[field].add("deletable_getset")

    # (3) __new__ / subclass bypass. The spec/designated forms keep the coarse
    # whole-file kill switch (a tp_new token anywhere disables them); the
    # positional form does precise per-type-block pairing instead.
    init_names: set[str] = set()
    if not _TP_NEW_TOKENS_RE.search(clean):
        init_names |= set(_TP_INIT_SLOT_RE.findall(clean))
        init_names |= set(_TP_INIT_DESIG_RE.findall(clean))
    init_names |= set(_positional_bypassable_inits(source))
    if not init_names:
        return nullable
    # Argument Clinic renames the real body to ``<slot>_impl``; accept both.
    init_fn_names = set()
    for n in init_names:
        init_fn_names.add(n)
        init_fn_names.add(n + "_impl")
    for func in functions:
        if func["name"] not in init_fn_names:
            continue
        recv = _receiver_name(func)
        if not recv:
            continue
        for field in _fields_set_in_init(func, recv):
            nullable[field].add("new_bypass")

    return nullable


def _first_arg(args_text: str) -> str:
    """Return the first top-level (paren-depth 0) argument of a call."""
    depth = 0
    for i, ch in enumerate(args_text):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            return args_text[:i].strip()
    return args_text.strip()


def _reads_foreign_struct(text: str, foreign: set[str]) -> bool:
    """True if ``base->field`` reads off a non-receiver parameter."""
    m = _FIELD_BASE_RE.search(text.strip())
    return m is not None and m.group(1) in foreign


def _resolve_target_field(
    target: str,
    nullable: dict[str, set[str]],
    aliases: dict[str, str],
    foreign: set[str],
) -> str | None:
    """Return the nullable field a sink argument reads, or None."""
    t = target.strip()
    if t in aliases:
        return aliases[t]
    m = _TRAILING_FIELD_RE.search(t)
    if m and m.group(1) in nullable and not _reads_foreign_struct(t, foreign):
        return m.group(1)
    return None


def _build_aliases(
    func: dict, nullable: dict[str, set[str]], source_bytes: bytes, foreign: set[str]
) -> dict[str, str]:
    """Map local vars assigned a bare ``recv->field`` read to that field."""
    aliases: dict[str, str] = {}
    for assign in find_assignments_in_scope(func["body_node"], source_bytes):
        var = assign["variable"]
        if not var.isidentifier():
            continue
        rhs = assign["value_text"].strip()
        m = _TRAILING_FIELD_RE.search(rhs)
        if not m or m.group(1) not in nullable:
            continue
        # Reject call results (``foo()->field``) — only plain reads / casts alias.
        if _CALL_TOKEN_RE.search(rhs):
            continue
        if _reads_foreign_struct(rhs, foreign):
            continue
        aliases[var] = m.group(1)
    return aliases


def _has_null_guard(body: str, field: str, aliases: dict[str, str]) -> bool:
    """True if ``body`` NULL-checks the field (or an alias of it) at all.

    Recognised guards: ``== NULL`` / ``!= NULL``; ``!field``; the truthiness
    idioms ``field &&`` / ``&& field)`` / ``if (field)`` / ``field ?``; and a
    ``CHECK_*`` macro over the field.

    Deliberately NOT guards: ``field != Py_None`` / ``Py_IsNone(field)`` — after
    an __init__ bypass the field is NULL, so those branches are still entered.
    The truthiness patterns require the field to be the *whole* boolean operand
    (terminated by ``)`` / ``&&`` / ``||`` / ``?``); ``&& field != Py_None`` is a
    comparison, not a truthiness check, and is correctly NOT matched.
    """
    names = {field} | {a for a, f in aliases.items() if f == field}
    for n in names:
        esc = re.escape(n)
        # field == NULL / field != NULL
        if re.search(rf"\b{esc}\b\s*(?:==|!=)\s*NULL", body):
            return True
        # !field  (but not !=, and not !field-> / !field.)
        if re.search(rf"!\s*(?:\w+\s*->\s*)?\b{esc}\b(?!\s*(?:=|->|\.))", body):
            return True
        # field &&  (short-circuit AND — the dominant CPython NULL-guard idiom)
        if re.search(rf"\b{esc}\b\s*&&", body):
            return True
        # ... && field  as a trailing boolean operand (followed by ) && || ?)
        if re.search(rf"&&\s*(?:\w+\s*->\s*)?\b{esc}\b\s*(?:\)|&&|\|\||\?)", body):
            return True
        # bare if (field) / if (self->field)
        if re.search(rf"\bif\s*\(\s*(?:\w+\s*->\s*)?\b{esc}\b\s*\)", body):
            return True
        # field ?  (ternary condition)
        if re.search(rf"\b{esc}\b\s*\?", body):
            return True
        # CHECK_* macro naming the field
        if re.search(rf"\bCHECK_\w+\s*\([^)]*\b{esc}\b", body):
            return True
    return False


def _sink_kind(name: str) -> str | None:
    if name in _ADDR_DEREF_SINKS:
        return "addr_deref"
    if name in _INCREF_SINKS:
        return "incref"
    if name in _CALL_SINKS:
        return "call"
    if name in _DEREF_SINKS:
        return "deref"
    return None


def _check_function(
    func: dict, nullable: dict[str, set[str]], source_bytes: bytes, tree
) -> list[dict]:
    """Flag unguarded sink reads of a nullable field in one function."""
    body = strip_comments(func["body"])
    foreign = _non_receiver_params(func)
    aliases = _build_aliases(func, nullable, source_bytes, foreign)

    findings: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for call in find_calls_in_scope(func["body_node"], source_bytes):
        name = call["function_name"]
        kind = _sink_kind(name)
        if kind is None:
            continue
        args = call["arguments_text"]
        if kind in ("call", "addr_deref"):
            target = _first_arg(args)
        else:
            target = args.strip()
        if kind == "addr_deref":
            # ``_PyBytes_Resize(&obj->field, n)`` — strip the address-of so the
            # field resolves.
            target = target.lstrip("&").strip()
        field = _resolve_target_field(target, nullable, aliases, foreign)
        if field is None:
            continue
        if _has_null_guard(body, field, aliases):
            continue
        line = call["start_line"]
        if is_suppressed_by_comment(source_bytes, tree, line):
            continue
        dedupe_key = (field, line)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        reasons = sorted(nullable[field])
        deletable = "deletable_member" in reasons or "deletable_getset" in reasons
        confidence = "high" if deletable else "medium"
        mechanism = (
            "a deletable member/getset (del obj.attr leaves it NULL)"
            if deletable
            else "a __new__/subclass bypass of tp_init (T.__new__(T) leaves it NULL)"
        )
        verb = {
            "incref": f"Py_INCREF'd via {name}()",
            "call": f"used as the callable in {name}()",
            "deref": f"dereferenced via {name}()",
            "addr_deref": f"passed by address to {name}(), which derefs *pv,",
        }[kind]
        findings.append(
            {
                "type": "init_bypass_null_deref",
                "function": func["name"],
                "field": field,
                "sink": name,
                "reason": ",".join(reasons),
                "line": line,
                "confidence": confidence,
                "detail": (
                    f"'{field}' is {verb} with no NULL guard, but it can be NULL "
                    f"via {mechanism}. A guard such as '{field} != Py_None' or "
                    f"'Py_IsNone({field})' does NOT protect this — after the "
                    f"bypass the field is NULL. Add 'if ({field} == NULL)' (or "
                    f"initialize it in tp_new) before the deref."
                ),
            }
        )
    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan for __init__-bypass NULL dereferences of nullable object fields."""
    project_root, scan_root = resolve_roots(target)

    findings: list[dict] = []
    total_functions = 0
    files_analyzed = 0
    files_with_nullable_fields = 0
    total_nullable_fields = 0
    nullable_by_reason: dict[str, int] = defaultdict(int)
    skipped: list[dict] = []

    for filepath in discover_c_files(scan_root, max_files=max_files):
        try:
            source_bytes = filepath.read_bytes()
        except OSError as e:
            skipped.append({"file": str(filepath), "reason": str(e)})
            continue

        try:
            tree = parse_bytes(source_bytes)
        except Exception as e:  # noqa: BLE001 - defensive parse guard
            skipped.append({"file": str(filepath), "reason": f"parse: {e}"})
            continue

        functions = extract_functions(tree, source_bytes)
        if not functions:
            continue

        files_analyzed += 1
        total_functions += len(functions)

        source = source_bytes.decode("utf-8", errors="replace")
        nullable = _collect_nullable_fields(source, functions)
        if not nullable:
            continue
        files_with_nullable_fields += 1
        total_nullable_fields += len(nullable)
        for reasons in nullable.values():
            for reason in reasons:
                nullable_by_reason[reason] += 1

        rel = relpath(filepath, project_root)
        for func in functions:
            for f in _check_function(func, nullable, source_bytes, tree):
                f["file"] = rel
                findings.append(f)

    findings = deduplicate_findings(findings)

    by_confidence: dict[str, int] = defaultdict(int)
    by_reason: dict[str, int] = defaultdict(int)
    for f in findings:
        by_confidence[f["confidence"]] += 1
        by_reason[f["reason"]] += 1

    return build_report(
        project_root=project_root,
        scan_root=scan_root,
        files_analyzed=files_analyzed,
        functions_analyzed=total_functions,
        findings=findings,
        summary={
            "total_findings": len(findings),
            "by_confidence": dict(by_confidence),
            "by_reason": dict(by_reason),
        },
        files_with_nullable_fields=files_with_nullable_fields,
        # Recall canary: zero findings with zero nullable fields means the rule
        # never fired on this corpus, NOT that the corpus is clean.
        total_nullable_fields=total_nullable_fields,
        nullable_fields_by_reason=dict(nullable_by_reason),
        skipped_files=skipped,
    )


def main() -> None:
    try:
        target, max_files = parse_common_args(sys.argv[1:])
        result = analyze(target, max_files=max_files)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:  # noqa: BLE001 - top-level JSON error envelope
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()

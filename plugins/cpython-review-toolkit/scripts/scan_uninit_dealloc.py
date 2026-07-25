#!/usr/bin/env python3
"""Scan CPython C source for dealloc of a half-constructed object on error.

The dangerous pattern (bug class B / O5): a constructor allocates an object
with a **non-zeroing** allocator (``PyObject_New`` / ``PyObject_GC_New`` /
``PyObject_NewVar`` …), then — before initializing the object's pointer members
— hits a fallible step and frees the object on the error path (``Py_DECREF`` /
``Py_XDECREF`` / ``Py_CLEAR``). The ``tp_dealloc`` / ``tp_clear`` then reads
those still-uninitialized members (garbage pointers) and crashes. Under
out-of-memory this is the dominant reachable-from-Python crash surface.

The predicate
-------------
The question is **not** "is there a ``= NULL`` somewhere before the free" (the
pre-v0.8 gate). It is:

    is a member written *after* the free, and not also written on a path that
    **dominates** the free?

Such a member holds allocator garbage at the moment ``tp_dealloc`` runs. The
dominance test is AST-based, not flat text: a write inside an ``if`` /
``else`` / ``switch`` / loop / ``#ifdef`` branch that the free is *not* inside
does not count as "written before". That branch-insensitivity is what hid
``PyList_New``'s free-threaded path (``op->ob_item = NULL`` lives in the
``size <= 0`` arm; the ``Py_DECREF(op)`` lives in the sibling arm) and would
hide gh-152851 (blake2) too.

A third filter keeps precision: only members the **destructor treats as a
pointer** count. A member qualifies when the same file contains
``Py_CLEAR(x->MEMBER)`` / ``Py_XDECREF(x->MEMBER)`` / ``Py_DECREF(x->MEMBER)``
/ ``Py_VISIT(x->MEMBER)``. Scalar members left uninitialized (``Py_ssize_t``
positions, cached flags, an interpreter-state pointer the clear never touches)
are not this bug shape.

Known false-positive classes this scanner deliberately stays silent on:

* **Zeroing allocators.** ``PyType_GenericAlloc`` / ``_PyType_AllocNoTrack`` /
  ``*_GC_Calloc`` zero the object, so a following early free is safe. They are
  simply absent from the allocator list — but a project-local *wrapper macro*
  can hide one, so an agent must still confirm the allocator on each finding.
  ``type->tp_alloc(type, n)`` is **not** unconditionally zeroing: a type may
  install its own ``allocfunc``. ``Modules/_datetimemodule.c`` does exactly
  that — ``time_alloc`` (:879) and ``datetime_alloc`` (:891) are
  ``PyObject_Malloc`` + ``_PyObject_Init`` with no ``memset``, and the file's
  own comment says "All data members remain uninitialized trash". The scanner
  therefore *resolves* the slot: :func:`_nonzeroing_tp_allocs` finds file-local
  ``tp_alloc`` implementations of that shape and, when a file has one, treats
  ``->tp_alloc(...)`` in that file as non-zeroing. (Those are the only two
  in-tree at 3.16.0a0; ``bytes_alloc``, ``_PyType_AllocNoTrack`` and
  ``PyType_GenericAlloc`` all zero. There is no live bug behind them today —
  every call site sets ``hastzinfo`` in the next statement — but the
  ``time_dealloc``/``datetime_dealloc`` switch on that scalar is the blake2
  ``impl`` shape exactly, so the assumption had to go.)
* **``Py_XDECREF``-guarded destructor + members already NULL.**
  ``Py_XDECREF(NULL)`` is a no-op; only a member left as *garbage* (not NULL)
  at the free point is a bug. That is exactly what the dominance test measures.
* **Wrapper constructors.** ``PyStructSequence_New`` (``Objects/structseq.c``)
  NULLs all ``n_fields`` slots before anything fallible — it is the guarded
  twin for this shape, and a *silent correct negative*, not an unexamined file.

Confirmed exemplars: ``template_iter`` (Objects/templateobject.c, gh-151815),
``odictiter_new`` (Objects/odictobject.c:1945 — reproduced, ``K=1`` OOM sweep
gives ``_PyObject_GC_UNTRACK`` assertion abort), ``PyList_New``
(Objects/listobject.c:250, free-threaded build), blake2 ``.copy()`` freeing an
object with an uninitialized ``impl`` enum (Modules/blake2, gh-152851).

Reproducibility note: a ``tp_dealloc`` that opens with the **unchecked macro**
``_PyObject_GC_UNTRACK`` faults on a never-tracked object *before* reading any
member, so those instances reproduce deterministically. One that uses the
checked function ``PyObject_GC_UnTrack`` only crashes when the recycled
allocator block is *dirty*; a clean OOM sweep on such a site is **not**
evidence the bug is fixed (gh-151815 is still live at 3.16.0a0 despite
sweeping 60/60 clean, because ``templateiter_clear`` NULLs both members before
``tp_free`` so same-type blocks always come back clean).

Usage:
    python scan_uninit_dealloc.py [path] [--max-files N]
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

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

# Allocators that do NOT zero the type-specific fields of the new object.
_NON_ZEROING_ALLOCATORS = (
    "PyObject_New",
    "PyObject_NewVar",
    "PyObject_GC_New",
    "PyObject_GC_NewVar",
    "_PyObject_New",
    "_PyObject_NewVar",
    "_PyObject_GC_New",
    "_PyObject_GC_NewVar",
    "PyObject_GC_NewWithExtra",
)

_NON_ZEROING_RE = re.compile(
    r"^\s*(?:\(\s*[\w\s\*]+\)\s*)?(" + "|".join(_NON_ZEROING_ALLOCATORS) + r")\s*\("
)

# A file-local ``allocfunc`` is non-zeroing when it hands back raw storage from
# one of these and never memsets it. ``PyObject_Calloc`` / ``PyMem_Calloc`` are
# absent on purpose: they zero.
_RAW_STORAGE_RE = re.compile(
    r"\b(?:PyObject_Malloc|PyObject_Realloc|PyMem_Malloc|PyMem_Realloc"
    r"|PyMem_RawMalloc|malloc|realloc)\s*\("
)
_ZEROES_RE = re.compile(r"\bmemset\s*\(|\bPyObject_Calloc\s*\(|\bPyMem_Calloc\s*\(")
# Slot registration for tp_alloc, all three CPython spellings. The positional
# form's only marker is the trailing comment, so this runs on raw source.
_TP_ALLOC_SLOT_RE = re.compile(
    r"^[ \t]*(?:\(\s*allocfunc\s*\)\s*)?(\w+)\s*,\s*/\*\s*tp_alloc\s*\*/"
    r"|\.tp_alloc\s*=\s*(?:\(\s*allocfunc\s*\)\s*)?(\w+)"
    r"|\{\s*Py_tp_alloc\s*,\s*(?:\(\s*allocfunc\s*\)\s*)?(\w+)",
    re.MULTILINE,
)
# `x->tp_alloc(...)` / `x.tp_alloc(...)` — the virtual call.
_TP_ALLOC_CALL_RE = re.compile(r"(?:->|\.)\s*tp_alloc\s*\(")
# Leading casts and redundant parens in front of the allocating call.
_LEAD_NOISE_RE = re.compile(r"^\s*(?:\(\s*(?:const\s+|struct\s+)*[\w\s\*]+\)\s*|\(\s*)")

_EARLY_FREE_NAMES: set[str] = {"Py_DECREF", "Py_XDECREF", "Py_CLEAR"}

# A member counts as "a pointer the destructor reads" when the file hands it to
# one of these macros. Py_VISIT is included because tp_traverse and tp_clear
# always agree on which members are owned references.
_DESTRUCTOR_MEMBER_RE = re.compile(
    rb"Py_(?:X?DECREF|CLEAR|VISIT)\s*\(\s*"
    rb"(?:\(\s*[A-Za-z_][\w\s*]*\)\s*)?"
    rb"[A-Za-z_]\w*\s*->\s*([A-Za-z_]\w*)"
)

# Secondary evidence: a member the destructor *reads* rather than decrefs. The
# blake2 exemplar (gh-152851) is exactly this — an uninitialized ``impl`` enum
# switched on during teardown — and so is the loop bound in
# ``elementiter_dealloc`` (``while (i--) Py_XDECREF(it->parent_stack[i]…)``
# with ``i`` read from an uninitialized ``parent_stack_used``).
_DESTRUCTOR_FN_RE = re.compile(r"dealloc|clear|traverse|finalize", re.IGNORECASE)
_TEARDOWN_BODY_RE = re.compile(r"Py_(?:X?DECREF|CLEAR|VISIT)\s*\(\s*\w+\s*->")

_BARE_IDENT_RE = re.compile(r"^\s*(?:\(\s*[\w\s\*]+\)\s*)*([A-Za-z_]\w*)\s*$")

# Node types whose children (other than the evaluated-first condition) run
# conditionally relative to the construct's parent. do_statement is absent on
# purpose: its body always executes at least once.
_BRANCHING_NODE_TYPES = frozenset(
    {
        "if_statement",
        "else_clause",
        "while_statement",
        "for_statement",
        "switch_statement",
        "case_statement",
        "conditional_expression",
    }
)

# Preprocessor conditionals are handled separately: only one arm is ever
# compiled, so a write in a plain ``#ifdef X … #endif`` block *does* dominate a
# later free within that build configuration. It fails to dominate only when
# the free sits in a *different arm* of the same conditional group.
_PREPROC_NODE_TYPES = frozenset(
    {
        "preproc_if",
        "preproc_ifdef",
        "preproc_else",
        "preproc_elif",
        "preproc_elifdef",
    }
)


def _memset_zero_re(var: str) -> re.Pattern:
    return re.compile(
        rf"memset\s*\(\s*(?:\(\s*void\s*\*\s*\)\s*)?{re.escape(var)}\b[^;]*,\s*0\s*,"
    )


def _bare_ident(text: str) -> str | None:
    """Return the identifier if ``text`` is a (possibly cast) single ident."""
    m = _BARE_IDENT_RE.match(text)
    return m.group(1) if m else None


def _member_evidence(
    source_bytes: bytes, functions: list[dict]
) -> dict[str, tuple[str, int]]:
    """Map member name -> (evidence kind, line) for members teardown cares about.

    Two kinds, in descending strength:

    ``pointer``
        Some function in the file does ``Py_CLEAR`` / ``Py_XDECREF`` /
        ``Py_DECREF`` / ``Py_VISIT`` on ``x->MEMBER``. The destructor owns a
        reference through it, so garbage there is a wild decref.
    ``destructor_read``
        The member is merely *read* inside an in-file destructor-shaped
        function (``*dealloc`` / ``*clear`` / ``*traverse`` / ``*finalize``)
        that also tears down at least one member reference. This catches the
        scalar discriminators and loop bounds that guard the teardown itself —
        blake2's ``impl`` enum (gh-152851) and ``elementiter_dealloc``'s
        ``parent_stack_used``.

    In-file evidence only: the constructor and the ``tp_dealloc`` of the type it
    builds live in the same file for essentially every CPython core type. A
    cross-file destructor is a known (and documented) recall hole.
    """
    out: dict[str, tuple[str, int]] = {}

    for func in functions:
        if not _DESTRUCTOR_FN_RE.search(func["name"]):
            continue
        if not _TEARDOWN_BODY_RE.search(func["body"]):
            continue
        for field in walk_descendants(func["body_node"], "field_expression"):
            name_node = field.child_by_field_name("field")
            if name_node is None:
                continue
            name = get_node_text(name_node, source_bytes)
            if name not in out:
                out[name] = ("destructor_read", field.start_point[0] + 1)

    for m in _DESTRUCTOR_MEMBER_RE.finditer(source_bytes):
        name = m.group(1).decode("ascii", "replace")
        line = source_bytes.count(b"\n", 0, m.start()) + 1
        if out.get(name, ("", 0))[0] != "pointer":
            out[name] = ("pointer", line)

    return out


# `type = &res->ht_type;` -- an interior pointer into the object just
# allocated. Every subsequent `type->tp_...` write is a write into `res`, and a
# rule keyed on the name `res` sees none of them.
# `Objects/typeobject.c:5628` writes 20+ fields of the new heap type through
# exactly this alias.
_INTERIOR_ALIAS_RE = re.compile(
    r"\b([A-Za-z_]\w*)\s*=\s*(?:\(\s*[\w\s*]+\)\s*)?&\s*([A-Za-z_]\w*)\s*->\s*\w+\s*;"
)
# `MyObject *obj = (MyObject *)self;` -- a plain cast alias.
_CAST_ALIAS_RE = re.compile(
    r"\b([A-Za-z_]\w*)\s*=\s*\(\s*[\w\s*]+\)\s*([A-Za-z_]\w*)\s*;"
)


def _alias_names(body: str, var: str) -> set[str]:
    """Locals that are interior-pointer or cast aliases of ``var``.

    Writes through an alias are writes to the object, so a rule that only knows
    the allocation's own variable name concludes "no members written" on a
    constructor that initialises twenty of them.
    """
    names = {var}
    for _ in range(3):
        grew = False
        for pattern in (_INTERIOR_ALIAS_RE, _CAST_ALIAS_RE):
            for m in pattern.finditer(body):
                if m.group(2) in names and m.group(1) not in names:
                    names.add(m.group(1))
                    grew = True
        if not grew:
            break
    return names


def _member_writes(
    body_node, source_bytes: bytes, var: str, aliases: set[str] | None = None
) -> list[tuple[str, Any]]:
    """Return ``[(member, assignment_node)]`` for every ``var->member = …``.

    ``aliases`` widens the receiver set to the object's interior-pointer and
    cast aliases (see :func:`_alias_names`).
    """
    receivers = aliases or {var}
    writes: list[tuple[str, Any]] = []
    for assign in walk_descendants(body_node, "assignment_expression"):
        left = assign.child_by_field_name("left")
        if left is None or left.type != "field_expression":
            continue
        argument = left.child_by_field_name("argument")
        field = left.child_by_field_name("field")
        if argument is None or field is None:
            continue
        if get_node_text(argument, source_bytes).strip() not in receivers:
            continue
        writes.append((get_node_text(field, source_bytes), assign))
    return writes


def _is_conditional_child(parent, child, free_ancestors: set[int]) -> bool:
    """True if ``child`` only runs on a branch the free is not on."""
    kind = parent.type
    if kind in _PREPROC_NODE_TYPES:
        # Only one arm of a preprocessor conditional is ever compiled, so a
        # write inside one arm still dominates a later free *outside* the whole
        # group. It fails to dominate only when the free is in a sibling arm —
        # detectable because the group node is then an ancestor of the free.
        return parent.id in free_ancestors
    if kind not in _BRANCHING_NODE_TYPES:
        return False
    if kind in ("if_statement", "while_statement", "switch_statement"):
        # The controlling expression is evaluated unconditionally.
        condition = parent.child_by_field_name("condition")
        return not (condition is not None and condition.id == child.id)
    if kind == "for_statement":
        initializer = parent.child_by_field_name("initializer")
        return not (initializer is not None and initializer.id == child.id)
    return True


def _dominates(write_node, free_node) -> bool:
    """True if ``write_node`` provably executes before ``free_node``.

    Structural dominance: the write must precede the free in source order and
    must not sit inside a branch (``if`` arm, ``switch`` case, loop body,
    sibling ``#ifdef`` arm) that the free is outside of.
    """
    if write_node.start_byte >= free_node.start_byte:
        return False

    free_ancestors: set[int] = set()
    node = free_node
    while node is not None:
        free_ancestors.add(node.id)
        node = node.parent

    node = write_node
    while node is not None and node.id not in free_ancestors:
        parent = node.parent
        if parent is None:
            return False
        if _is_conditional_child(parent, node, free_ancestors):
            return False
        node = parent
    return node is not None


def _nonzeroing_tp_allocs(source: str, functions: list[dict]) -> set[str]:
    """File-local ``tp_alloc`` implementations that leave members as garbage.

    Resolve the slot rather than trusting the name: ``type->tp_alloc(type, n)``
    is only zeroing when it resolves to ``PyType_GenericAlloc`` (or another
    zeroing allocator).  A type that installs its own ``allocfunc`` built from
    ``PyObject_Malloc`` + ``_PyObject_Init`` hands back uninitialized storage —
    ``Modules/_datetimemodule.c`` ``time_alloc`` / ``datetime_alloc`` are the
    two in-tree instances at 3.16.0a0.
    """
    registered = {
        name
        for m in _TP_ALLOC_SLOT_RE.finditer(source)
        for name in m.groups()
        if name and name not in ("0", "NULL")
    }
    if not registered:
        return set()
    out: set[str] = set()
    for func in functions:
        if func["name"] not in registered:
            continue
        body = func.get("body", "")
        if _RAW_STORAGE_RE.search(body) and not _ZEROES_RE.search(body):
            out.add(func["name"])
    return out


def _strip_lead(text: str) -> str:
    """Drop leading casts and redundant parens: ``(T *) (x->tp_alloc(...))``."""
    prev = None
    while prev != text:
        prev = text
        text = _LEAD_NOISE_RE.sub("", text, count=1)
    return text


def _matched_allocator(value_text: str, local_nonzeroing: set[str]) -> str | None:
    """Name the non-zeroing allocator this initializer calls, if any."""
    m = _NON_ZEROING_RE.match(value_text)
    if m is not None:
        return m.group(1)
    head = _strip_lead(value_text)
    for name in local_nonzeroing:
        if re.match(r"\b" + re.escape(name) + r"\s*\(", head):
            return name
    # `<type>->tp_alloc(...)` in a file that installs a non-zeroing allocfunc:
    # the slot may resolve to it, so it is no longer assumed to zero.
    if local_nonzeroing and re.match(
        r"\w+\s*(?:->|\.)\s*tp_alloc\s*\(", head
    ):
        return "tp_alloc"
    return None


def _check_function(
    func: dict,
    source_bytes: bytes,
    tree,
    evidence: dict[str, tuple[str, int]],
    local_nonzeroing: set[str] | None = None,
) -> list[dict]:
    """Flag non-zeroing constructors that free an object with garbage members."""
    findings: list[dict] = []
    body_node = func["body_node"]

    frees = sorted(
        find_calls_in_scope(body_node, source_bytes, _EARLY_FREE_NAMES),
        key=lambda c: c["start_byte"],
    )
    if not frees:
        return findings

    for assign in find_assignments_in_scope(body_node, source_bytes):
        var = assign["variable"]
        if not var.isidentifier():
            continue
        api = _matched_allocator(assign["value_text"], local_nonzeroing or set())
        if api is None:
            continue
        alloc_end = assign["value_node"].end_byte

        # First early free of this object after the allocation. Only the first
        # one is considered: a later free is normally dominated by the writes
        # the earlier error path skipped, and reporting both double-counts one
        # constructor.
        free = None
        for call in frees:
            if call["start_byte"] < alloc_end:
                continue
            if _bare_ident(call["arguments_text"]) != var:
                continue
            free = call
            break
        if free is None:
            continue
        free_node = free["node"]

        # An explicit memset of the whole object between the allocation and the
        # free zeroes every member -> safe.
        window = source_bytes[alloc_end : free_node.start_byte].decode(
            "utf-8", "replace"
        )
        if _memset_zero_re(var).search(window):
            continue

        aliases = _alias_names(func.get("body", ""), var)
        writes = [
            (member, node)
            for member, node in _member_writes(body_node, source_bytes, var, aliases)
            if node.start_byte >= alloc_end
        ]
        if not writes:
            continue

        written_before = {
            member for member, node in writes if _dominates(node, free_node)
        }
        written_after = {
            member for member, node in writes if node.start_byte > free_node.end_byte
        }
        unset_at_free = sorted(written_after - written_before)
        if not unset_at_free:
            continue

        # Keep only members the file's destructor actually cares about.
        kept = [m for m in unset_at_free if m in evidence]
        if not kept:
            continue
        destructor_evidence: dict[str, dict[str, Any]] = {
            m: {"kind": evidence[m][0], "line": evidence[m][1]} for m in kept
        }
        has_pointer = any(v["kind"] == "pointer" for v in destructor_evidence.values())

        line = assign["start_line"]
        if is_suppressed_by_comment(source_bytes, tree, line):
            continue

        members = ", ".join(f"{var}->{m}" for m in kept)
        evidence_line = min(v["line"] for v in destructor_evidence.values())
        role = (
            "decrefs/visits them"
            if has_pointer
            else "reads them while tearing down other members"
        )
        findings.append(
            {
                "type": "dealloc_of_uninitialized_object",
                "function": func["name"],
                "variable": var,
                "allocator": api,
                "line": line,
                "free_line": free["start_line"],
                "free_call": free["function_name"],
                "unset_members": kept,
                "unset_members_all": unset_at_free,
                "receiver_aliases": sorted(aliases - {var}),
                "destructor_evidence": destructor_evidence,
                "confidence": "medium" if has_pointer else "low",
                "detail": (
                    f"'{var}' is allocated via the non-zeroing {api}() and freed "
                    f"with {free['function_name']}({var}) at line "
                    f"{free['start_line']} while {members} still hold allocator "
                    f"garbage — those members are written only *after* the free "
                    f"(or only on a branch that does not dominate it), and the "
                    f"file's destructor {role} (see line {evidence_line}). "
                    f"tp_dealloc/tp_clear then acts on an uninitialized value "
                    f"(crash, dominant under OOM). Fix: NULL/initialize these "
                    f"members immediately after the allocation, before the "
                    f"first fallible call."
                ),
            }
        )

    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan for constructors that free half-initialized objects on error."""
    project_root, scan_root = resolve_roots(target)

    findings: list[dict] = []
    total_functions = 0
    files_analyzed = 0
    skipped: list[dict] = []
    # Denominators for a zero result. `allocation_sites` counts every
    # non-zeroing allocation the rule saw at all; `nonzeroing_tp_allocs` names
    # the file-local allocfuncs that made a `tp_alloc(...)` call count as
    # non-zeroing. Zero findings against zero sites is silence, not safety.
    allocation_sites = 0
    nonzeroing_by_file: dict[str, list[str]] = {}

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
        evidence = _member_evidence(source_bytes, functions)
        local_nonzeroing = _nonzeroing_tp_allocs(
            source_bytes.decode("utf-8", "replace"), functions
        )
        if local_nonzeroing:
            nonzeroing_by_file[rel] = sorted(local_nonzeroing)

        for func in functions:
            total_functions += 1
            for assign in find_assignments_in_scope(func["body_node"], source_bytes):
                if _matched_allocator(assign["value_text"], local_nonzeroing):
                    allocation_sites += 1
            for f in _check_function(
                func, source_bytes, tree, evidence, local_nonzeroing
            ):
                f["file"] = rel
                findings.append(f)

    findings = deduplicate_findings(findings)

    by_allocator: dict[str, int] = defaultdict(int)
    for f in findings:
        by_allocator[f["allocator"]] += 1

    return build_report(
        project_root=project_root,
        scan_root=scan_root,
        files_analyzed=files_analyzed,
        functions_analyzed=total_functions,
        findings=findings,
        summary={
            "total_findings": len(findings),
            "by_allocator": dict(by_allocator),
            "allocation_sites": allocation_sites,
        },
        nonzeroing_tp_allocs=nonzeroing_by_file,
        # The zeroing model, stated so a zero result is readable and so a future
        # change cannot quietly invert it. `PyType_GenericAlloc` is *not* in the
        # non-zeroing set on purpose: it forwards to `_PyType_AllocNoTrack`,
        # which does `memset((char *)obj + sizeof(PyObject), 0, ...)` at
        # Objects/typeobject.c:2542. Treating `tp_alloc` as unconditionally
        # non-zeroing would model a falsehood and manufacture a finding on every
        # heap-type constructor in the tree.
        allocator_model={
            "non_zeroing": sorted(_NON_ZEROING_ALLOCATORS),
            "zeroing_by_evidence": {
                "PyType_GenericAlloc": (
                    "forwards to _PyType_AllocNoTrack, which memsets the object "
                    "after the PyObject header (Objects/typeobject.c:2542)"
                ),
            },
            "tp_alloc_rule": (
                "a `->tp_alloc(...)` call counts as non-zeroing only in a file "
                "that registers its own allocfunc returning raw storage; the "
                "two in-tree instances are Modules/_datetimemodule.c "
                "time_alloc / datetime_alloc"
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

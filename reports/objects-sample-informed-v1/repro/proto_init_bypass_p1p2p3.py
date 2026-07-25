#!/usr/bin/env python3
"""Scan CPython C source for __init__-bypass NULL dereferences.

The dangerous pattern: a C method/slot reads ``self->field`` and INCREFs it,
calls it, or dereferences it with **no NULL check**, but the field can
legitimately be NULL because construction can bypass the ``tp_init`` that would
have set it. There are two ways the field becomes NULL:

1. **``__new__`` / subclass bypass.** The type wires a ``tp_init`` that assigns
   ``self->field`` but has no ``tp_new`` (and no ``DISALLOW_INSTANTIATION``), so
   ``T.__new__(T)`` produces a zeroed object on which ``field`` is NULL and
   ``__init__`` never ran.
2. **A deletable member/getset.** The field is exposed via a ``PyMemberDef`` with
   a deletable object type (``T_OBJECT`` / ``T_OBJECT_EX`` and the ``Py_``/``_Py_``
   spellings, without ``READONLY``), so ``del obj.field`` leaves it NULL.

A guard like ``if (self->field != Py_None)`` or ``if (!Py_IsNone(self->field))``
is **not** a NULL guard: after the bypass the field is NULL, ``NULL != Py_None``
is true, and control enters the crashing block.

Confirmed exemplars this targets:
  - gh-152954 — ``sqlite3.Connection.__new__`` bypass leaves ``row_factory`` NULL,
    then ``Py_INCREF(self->row_factory)`` crashes (Modules/_sqlite/connection.c).
  - gh-152817 — ``del cursor.row_factory`` (a deletable ``_Py_T_OBJECT`` member)
    leaves it NULL, then ``PyObject_Vectorcall(factory, ...)`` crashes
    (Modules/_sqlite/cursor.c).

The scanner is deliberately high-recall: it flags candidates, an agent confirms
the field can really be NULL and that the deref is genuinely unguarded. It
cannot see cross-function guarantees (a caller that always sets the field).

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
# reads) and the flags text so READONLY members can be filtered.
_MEMBER_ENTRY_RE = re.compile(
    r'\{\s*"[^"]*"\s*,\s*'
    r"(" + "|".join(_DELETABLE_MEMBER_TYPES) + r")\s*,\s*"
    r"(?:offsetof\s*\(\s*\w+\s*,\s*(\w+)\s*\)|[A-Z_]\w*\s*\(\s*(\w+)\s*\))\s*,\s*"
    r"([^}]*)\}"
)

# tp_init wiring (PyType_Slot form and static-PyTypeObject designated form).
_TP_INIT_SLOT_RE = re.compile(r"\bPy_tp_init\s*,\s*(?:&\s*)?(\w+)")
_TP_INIT_DESIG_RE = re.compile(
    r"\.tp_init\s*=\s*(?:\(\s*[\w\s\*]+\)\s*)?(?:&\s*)?(\w+)"
)
# Any of these tokens means the type controls instantiation itself (a real
# tp_new, or an explicit disallow) — the __new__-bypass reasoning does not apply,
# so we conservatively disable the bypass signal for the whole file.
_TP_NEW_TOKENS_RE = re.compile(r"\bPy_tp_new\b|\.tp_new\s*=|\bDISALLOW_INSTANTIATION\b")
_TP_INIT_POS_RE = re.compile(r"^[ \t]*(\w+)\s*,\s*/\*\s*tp_init\s*\*/", re.M)
_TP_NEW_POS_RE = re.compile(r"^[ \t]*(\w+)\s*,\s*/\*\s*tp_new\s*\*/", re.M)
_INHERITED_NEW = {"PyType_GenericNew"}

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
# PyObject**-taking APIs that dereference *pv with NO NULL check. Their siblings
# _PyTuple_Resize ("v == NULL ||") and PyBytes_Concat ("if (*pv == NULL) return")
# DO check, so only the unguarded ones belong here.
_ADDR_DEREF_SINKS = frozenset({"_PyBytes_Resize"})

# RHS ends in ``-> field`` (possibly through casts), e.g. ``self->row_factory``
# or ``((FooObject *)self)->row_factory``.
_TRAILING_FIELD_RE = re.compile(r"->\s*(\w+)\s*$")
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


def _fields_set_in_init(func: dict, recv: str) -> set[str]:
    """Return struct fields the init function assigns via ``recv->field``."""
    body = strip_comments(func["body"])
    esc = re.escape(recv)
    fields: set[str] = set()
    # Direct assignment: recv->field = ...  (but not ==, >=, <=, !=).
    for m in re.finditer(rf"\b{esc}\s*->\s*(\w+)\s*=(?!=)", body):
        fields.add(m.group(1))
    # Py_SETREF / Py_XSETREF(recv->field, ...).
    for m in re.finditer(rf"Py_X?SETREF\s*\(\s*{esc}\s*->\s*(\w+)", body):
        fields.add(m.group(1))
    return fields


def _collect_nullable_fields(source: str, functions: list[dict]) -> dict[str, set[str]]:
    """Map ``field_name -> {reasons}`` for fields that can legitimately be NULL.

    Reasons: ``deletable_member`` (a deletable PyMemberDef entry) and
    ``new_bypass`` (assigned only in a tp_init with no tp_new to protect it).
    """
    clean = strip_comments(source)
    nullable: dict[str, set[str]] = defaultdict(set)

    # (1) Deletable members.
    for m in _MEMBER_ENTRY_RE.finditer(clean):
        _mtype, field, flags = m.group(1), (m.group(2) or m.group(3)), m.group(4)
        if "READONLY" in flags:
            continue
        nullable[field].add("deletable_member")

    # (2) __new__ / subclass bypass — only when the file wires a tp_init but no
    # tp_new / DISALLOW_INSTANTIATION anywhere.
    # Per-PyTypeObject-block pairing: a tp_init is only bypassable when the
    # tp_new in the SAME slot table is 0 (inherited) or PyType_GenericNew.
    pos_inits = []
    news = [(m.start(), m.group(1)) for m in _TP_NEW_POS_RE.finditer(source)]
    for m in _TP_INIT_POS_RE.finditer(source):
        iv = m.group(1)
        if iv == "0":
            continue
        nv = next((v for pos, v in news if pos > m.start()), None)
        if nv is None or nv == "0" or nv in _INHERITED_NEW:
            pos_inits.append(iv)
    if _TP_NEW_TOKENS_RE.search(clean) and not pos_inits:
        return nullable
    init_names = set(_TP_INIT_SLOT_RE.findall(clean))
    init_names |= set(_TP_INIT_DESIG_RE.findall(clean))
    init_names |= set(pos_inits)
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


def _resolve_target_field(
    target: str, nullable: dict[str, set[str]], aliases: dict[str, str]
) -> str | None:
    """Return the nullable field a sink argument reads, or None."""
    t = target.strip()
    if t in aliases:
        return aliases[t]
    m = _TRAILING_FIELD_RE.search(t)
    if m and m.group(1) in nullable:
        return m.group(1)
    return None


def _build_aliases(
    func: dict, nullable: dict[str, set[str]], source_bytes: bytes
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
    aliases = _build_aliases(func, nullable, source_bytes)

    findings: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for call in find_calls_in_scope(func["body_node"], source_bytes):
        name = call["function_name"]
        kind = _sink_kind(name)
        if kind is None:
            continue
        args = call["arguments_text"]
        if kind == "addr_deref":
            target = _first_arg(args).lstrip("&").strip()
        else:
            target = _first_arg(args) if kind == "call" else args.strip()
        field = _resolve_target_field(target, nullable, aliases)
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
        confidence = "high" if "deletable_member" in reasons else "medium"
        mechanism = (
            "a deletable member/getset (del obj.attr leaves it NULL)"
            if "deletable_member" in reasons
            else "a __new__/subclass bypass of tp_init (T.__new__(T) leaves it NULL)"
        )
        verb = {
            "incref": f"Py_INCREF'd via {name}()",
            "call": f"used as the callable in {name}()",
            "deref": f"dereferenced via {name}()",
            "addr_deref": f"passed by address to {name}(), which derefs *pv unguarded",
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
        total_nullable_fields=total_nullable_fields,
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

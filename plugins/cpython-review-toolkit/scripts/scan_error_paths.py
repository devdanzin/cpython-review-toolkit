#!/usr/bin/env python3
"""Scan CPython C source for error handling bugs.

Rules emitted (``findings[].type``):

``missing_null_check``
    A fallible API result is dereferenced before any NULL test.
``unchecked_return``
    A fallible API result is neither checked nor returned unmodified.
``alloc_null_no_memerror``
    A *raw* allocator (``PyMem_Malloc``, ``malloc``, ... — allocators that do
    **not** set ``MemoryError`` themselves) fails and the guarded branch
    returns an error sentinel without calling ``PyErr_NoMemory()`` or any
    other ``PyErr_Set*``.  Replaces the old, unusably broad
    ``return_null_no_exception`` rule.
``unconditional_pyerr_clear``
    ``PyErr_Clear()`` with no ``PyErr_ExceptionMatches`` / save-restore call
    within the preceding few lines: the clear swallows *whatever* exception is
    live, including ``KeyboardInterrupt`` and ``MemoryError``.  Destructor-family
    functions are deliberately skipped — those belong to ``scan_pyerr_clear.py``.
``pylong_sentinel_no_errcheck``
    ``PyLong_As*`` compared against ``-1`` with no ``PyErr_Occurred()``
    narrowing on the guard.  ``-1`` is both the error sentinel *and* the honest
    conversion of the integer ``-1``, so the bare comparison makes the function
    return its error sentinel with **no exception set**.
``unchecked_parse``
    ``PyArg_ParseTuple*`` result not tested.

Two rules were retired.  ``return_null_no_exception`` was replaced by
``alloc_null_no_memerror`` (see above).  ``sparse_error_cleanup`` scored 0/6 on
``Objects/`` and 0/4 after gating — the CPython idiom releases each owned
reference *before* its ``goto``, leaving nothing at the label, so the rule
cannot tell correct code from incomplete cleanup.

Usage:
    python scan_error_paths.py [path]

    path: directory, file, or omitted for current directory
"""

import json
import re
import sys
from pathlib import Path
from typing import Generator


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def find_cpython_root(start: Path) -> Path | None:
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


_EXCLUDE_DIRS = frozenset({
    ".git", ".tox", ".venv", "venv", "__pycache__",
    "node_modules", "build", "dist", ".eggs",
})


def discover_c_files(
    root: Path, *, max_files: int = 0,
) -> Generator[Path, None, None]:
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
    """Blank out comments and string literals, preserving line structure.

    Every newline inside a ``/* ... */`` block is preserved so that offsets
    computed on the stripped text still map onto the original line numbers.
    """
    source = re.sub(
        r'/\*.*?\*/',
        lambda m: " " + "\n" * m.group(0).count("\n"),
        source,
        flags=re.DOTALL,
    )
    source = re.sub(r'//[^\n]*', ' ', source)
    source = re.sub(r'"(?:[^"\\]|\\.)*"', '""', source)
    source = re.sub(r"'(?:[^'\\]|\\.)*'", "''", source)
    return source


# ---------------------------------------------------------------------------
# APIs that can return NULL on error
# ---------------------------------------------------------------------------

NULL_RETURN_APIS = frozenset({
    "PyObject_Call", "PyObject_CallObject", "PyObject_CallFunction",
    "PyObject_CallMethod", "PyObject_CallNoArgs", "PyObject_CallOneArg",
    "PyObject_GetAttr", "PyObject_GetAttrString",
    "PyObject_GetItem", "PyObject_Str", "PyObject_Repr",
    "PyObject_Bytes", "PyObject_RichCompare", "PyObject_Format",
    "PyUnicode_FromString", "PyUnicode_FromFormat", "PyUnicode_Decode",
    "PyUnicode_Join", "PyUnicode_FromObject",
    "PyBytes_FromString", "PyBytes_FromStringAndSize",
    "PyLong_FromLong", "PyLong_FromDouble",
    "PyFloat_FromDouble",
    "PyList_New", "PyList_GetSlice",
    "PyTuple_New", "PyTuple_GetSlice", "PyTuple_Pack",
    "PyDict_New", "PyDict_Copy", "PyDict_Keys", "PyDict_Values",
    "PySequence_List", "PySequence_Tuple", "PySequence_GetItem",
    "PyNumber_Add", "PyNumber_Subtract", "PyNumber_Multiply",
    "PyIter_Next",
    "PyImport_ImportModule", "PyImport_Import",
    "PyModule_New", "PyModule_NewObject",
    "Py_BuildValue",
    "PyErr_Format", "PyErr_NewException",
    "PyCapsule_New",
    "PyMem_Malloc", "PyMem_Realloc", "PyObject_Malloc",
    "PyObject_Realloc",
    "malloc", "calloc", "realloc",
})

PYERR_SET_APIS = frozenset({
    "PyErr_SetString", "PyErr_Format", "PyErr_SetNone",
    "PyErr_SetObject", "PyErr_NoMemory", "PyErr_BadArgument",
    "PyErr_BadInternalCall", "PyErr_SetFromErrno",
    "PyErr_SetFromErrnoWithFilename",
    "PyErr_SetFromWindowsErr",
    "PyErr_SetRaisedException", "PyErr_Restore",
    "_PyErr_SetString", "_PyErr_Format", "_PyErr_SetObject",
    "_PyErr_NoMemory", "_PyErr_SetRaisedException",
})


# ---------------------------------------------------------------------------
# Allocators
# ---------------------------------------------------------------------------
#
# RAW_ALLOCATORS return NULL *without* setting MemoryError; the caller owes an
# explicit PyErr_NoMemory().  Note that PyMem_New / PyMem_Resize are plain
# macros over PyMem_Malloc / PyMem_Realloc (Include/pymem.h:63,73) and so
# belong here, not in the exempt set.
RAW_ALLOCATORS = frozenset({
    "PyMem_Malloc", "PyMem_Calloc", "PyMem_Realloc",
    "PyMem_RawMalloc", "PyMem_RawCalloc", "PyMem_RawRealloc",
    "PyMem_New",
    "PyObject_Malloc", "PyObject_Calloc", "PyObject_Realloc",
    "malloc", "calloc", "realloc", "strdup",
})

# Allocators that set MemoryError themselves — never flagged by
# alloc_null_no_memerror.  Kept as documentation for triage and for the
# taxonomy entry in data/cpython_non_bugs.md.
EXCEPTION_SETTING_ALLOCATORS = frozenset({
    "PyObject_New", "PyObject_NewVar", "PyObject_GC_New", "PyObject_GC_NewVar",
    "_PyObject_New", "_PyObject_NewVar", "_PyObject_GC_New",
    "_PyObject_GC_NewVar", "PyType_GenericAlloc", "_PyType_AllocNoTrack",
    "PyList_New", "PyTuple_New", "PyDict_New", "PyByteArray_FromObject",
    "PyBytes_FromStringAndSize", "PyUnicode_New",
})


# ---------------------------------------------------------------------------
# Function detection
# ---------------------------------------------------------------------------

def find_functions(source: str) -> list[dict]:
    lines = source.split('\n')
    functions: list[dict] = []
    for i, line in enumerate(lines):
        if not line.startswith('{'):
            continue
        if i < 1:
            continue
        prev = lines[i - 1].strip()
        m = re.match(r'^(\w+)\s*\(([^)]*)\)\s*$', prev)
        if not m:
            m = re.match(r'^(?:\w[\w\s\*]*?)\s+(\w+)\s*\(([^)]*)\)\s*$', prev)
        if not m:
            continue
        func_name = m.group(1)
        if func_name in ('if', 'for', 'while', 'switch', 'do', 'else',
                         'sizeof', 'return', 'typedef', 'struct', 'union',
                         'enum', 'defined'):
            continue

        depth = 1
        body_start = i + 1
        body_end = body_start
        for j in range(body_start, len(lines)):
            for ch in lines[j]:
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        body_end = j
                        break
            if depth == 0:
                break

        body = '\n'.join(lines[body_start:body_end])
        sig_start = i - 1
        if sig_start > 0 and re.match(
            r'^[\w\s\*]+$', lines[sig_start - 1].strip()
        ):
            sig_start -= 1

        # Detect the return type to classify the error convention.  The type
        # is built from the signature lines *themselves* (lines[sig_start:i]);
        # reading lines[sig_start - 1] would pick up the line above the return
        # type — usually a blank line or the previous function's closing brace.
        sig_text = " ".join(ln.strip() for ln in lines[sig_start:i])
        idx = sig_text.find(func_name)
        ret_type = sig_text[:idx].strip() if idx > 0 else ""

        functions.append({
            "name": func_name,
            "body": body,
            "start_line": sig_start + 1,
            # 1-based line number of the first line of the body, i.e. the line
            # a zero offset into `body` corresponds to.
            "body_start_line": body_start + 1,
            "end_line": body_end + 1,
            "return_type": ret_type,
        })
    return functions


# ---------------------------------------------------------------------------
# Error path analysis
# ---------------------------------------------------------------------------

def _alt(names) -> str:
    return '|'.join(re.escape(n) for n in sorted(names, key=len, reverse=True))


# An lvalue: a bare identifier, or a struct-member chain such as `ub->args`
# or `self->dict.ob_item` (FP class F — struct-member LHS).
_LVALUE = r'\w+(?:\s*(?:->|\.)\s*\w+){0,4}'
# An optional cast, e.g. `(char *)PyMem_Malloc(n)`.
_CAST = r'(?:\(\s*(?:const\s+|unsigned\s+|struct\s+)*\w+\s*\**\s*\)\s*)?'
# One or more chained assignment targets: `args = tuple_args = ...`
# (FP class D — multi-assignment alias).
_ASSIGN_CHAIN = r'((?:' + _LVALUE + r'\s*=\s*){1,4})'

_ASSIGN_CALL_RE = re.compile(
    _ASSIGN_CHAIN + _CAST + r'(' + _alt(NULL_RETURN_APIS) + r')\s*\('
)
_ALLOC_CALL_RE = re.compile(
    _ASSIGN_CHAIN + _CAST + r'(' + _alt(RAW_ALLOCATORS) + r')\s*\('
)

_ERROR_RETURN_RE = re.compile(r'\breturn\s+(?:NULL|-1|0|false)\s*;')
_GOTO_RE = re.compile(r'\bgoto\s+(\w+)\s*;')
_PYERR_RE = re.compile(r'\b(' + _alt(PYERR_SET_APIS) + r')\s*\(')
_PYARG_PARSE_RE = re.compile(
    r'(\w+)\s*=\s*PyArg_Parse(?:Tuple|TupleAndKeywords)\s*\('
)
_SETREF_RE = re.compile(
    r'\bPy_X?SETREF\s*\(\s*(' + _LVALUE + r')\s*,\s*(' + _LVALUE + r')\s*\)'
)

# PyErr_Clear rule.
_PYERR_CLEAR_RE = re.compile(r'\b_?PyErr_Clear\s*\(')
# `PyErr_Occurred` is deliberately NOT in this alternation.  It answers "is
# *something* pending", never "is it the exception I expected" — it is the
# failure test itself, not a narrowing.  Counting it as a guard silently
# suppressed every `if (x == -1 && PyErr_Occurred()) PyErr_Clear();`, which is
# the single most common written form of the bug this rule exists to find
# (measured: Modules/ 39 -> 46, +4 true positives / +1 false positive on the
# 12-file informed sample).  The one known residual FP class is a `PyLong_As*`
# clear on an operand the function already `PyLong_Check`-ed, where only an
# OverflowError can be pending (Modules/itertoolsmodule.c count_repr).
_PYERR_CLEAR_GUARD_RE = re.compile(
    r'\b_?PyErr_(?:ExceptionMatches|GivenExceptionMatches|GetRaisedException'
    r'|Fetch|SetRaisedException|Restore|GetHandledException)\s*\('
)
# A test whose failure branch the clear sits in — the shape the rule is about.
_FAILURE_TEST_RE = re.compile(
    r'(?:==\s*NULL|!=\s*NULL|==\s*-1|<\s*0|!=\s*0|==\s*0|\bif\s*\(\s*!)'
)
# Destructor family — owned by scan_pyerr_clear.py, skipped here on purpose.
_DESTRUCTOR_RE = re.compile(
    r'(?:dealloc|_free$|finalize|tp_clear|_clear$|__del__)', re.IGNORECASE
)

# Consumers documented to tolerate NULL and propagate the pending exception:
# passing an unchecked result to one of these is a CPython idiom, not a bug.
# (Python/modsupport.c:602 — PyModule_AddObjectRef rejects NULL explicitly.)
_NULL_TOLERANT_CONSUMER = (
    r'\b(?:PyModule_Add(?:Object(?:Ref)?|Type)?|Py_XDECREF|Py_CLEAR'
    r'|Py_XSETREF|Py_XINCREF|Py_XNewRef)\s*\([^;]{0,200}?'
)

# How far ahead of an assignment to look for an immediate dereference.
_DEREF_WINDOW = 200


def _lvalue_pattern(target: str) -> str:
    """Turn a captured lvalue into a whitespace-tolerant regex."""
    parts = [p for p in re.split(r'\s*(?:->|\.)\s*', target.strip()) if p]
    if not parts:
        return r'(?!)'
    return r'\b' + r'\s*(?:->|\.)\s*'.join(re.escape(p) for p in parts) + r'\b'


def _normalize(target: str) -> str:
    return re.sub(r'\s+', '', target)


def _split_targets(chain: str) -> list[str]:
    """`'args = tuple_args = '` -> `['args', 'tuple_args']`."""
    return [t.strip() for t in chain.split('=') if t.strip()]


def _check_re(patterns: list[str]) -> re.Pattern:
    return re.compile('|'.join(patterns))


def _null_check_patterns(vp: str) -> list[str]:
    """Every spelling that counts as "this value was tested".

    Covers FP class B (positive-form checks): `if (v)`, `if (v != NULL)`,
    `while ((v = f()))`, `v == NULL ? ... : ...`.
    """
    pre = r'[!*&(\s]*'
    return [
        vp + r'\s*(?:==|!=)\s*(?:NULL|0)\b',
        r'if\s*\(' + pre + vp + r'\s*[)&|,]',
        r'while\s*\(' + pre + vp + r'\s*[)&|]',
        r'while\s*\([^)]*' + vp + r'\s*=[^=]',
        r'for\s*\([^;]*;[^;]*' + vp + r'\s*[;)&|]',
        r'assert\s*\(' + pre + vp + r'\s*[)&|]',
        r'\bif\s*\([^;{}]{0,200}?' + vp + r'\s*(?:==|!=)\s*NULL',
    ]


def _in_condition(clean: str, pos: int) -> bool:
    """Is `pos` inside an `if (...)` / `while (...)` / `for (...)` header?

    `while ((key = PyIter_Next(it)) != NULL)` and
    `if ((tmp = PyNumber_Subtract(a, b)) == NULL)` assign *and* test in one
    expression — FP class B, the positive-form/loop-condition check.
    """
    depth = 0
    semis = 0
    i = pos - 1
    limit = max(0, pos - 800)
    while i >= limit:
        ch = clean[i]
        if ch == ')':
            depth += 1
        elif ch == '(':
            if depth == 0:
                head = clean[max(0, i - 16):i]
                km = re.search(r'\b(if|while|for|switch)\s*$', head)
                if km is not None:
                    # Crossing a ';' is only legitimate inside a for-header.
                    return semis == 0 or km.group(1) == 'for'
            else:
                depth -= 1
        elif depth == 0:
            if ch in '{}':
                return False
            if ch == ';':
                semis += 1
                if semis > 2:
                    return False
        i -= 1
    return False


def _is_out_param_assign(clean: str, start: int) -> bool:
    """Is this `*out = API(...)` (store through a caller-supplied pointer)?

    Distinguished from the declaration `PyObject *out = API(...)`, where the
    `*` belongs to the type.  Out-parameter stores hand the NULL check to the
    caller by contract, so they are not local bugs.
    """
    j = start - 1
    while j >= 0 and clean[j] in ' \t':
        j -= 1
    if j < 0 or clean[j] != '*':
        return False
    j -= 1
    while j >= 0 and clean[j] in ' \t':
        j -= 1
    return j < 0 or not (clean[j].isalnum() or clean[j] == '_')


def _resolve_aliases(targets: list[str], after: str) -> list[str]:
    """Add `Py_SETREF`/`Py_XSETREF` destinations (FP class C).

    `Py_SETREF(item, tmp); if (item == NULL)` — the value came back in `tmp`
    but the check is written against `item`.
    """
    aliases = list(targets)
    known = {_normalize(t) for t in targets}
    for sm in _SETREF_RE.finditer(after):
        dest, src = sm.group(1), sm.group(2)
        if _normalize(src) in known and _normalize(dest) not in known:
            aliases.append(dest)
            known.add(_normalize(dest))
    return aliases


_RESULT_GUARD_TEMPLATE = (
    r'if\s*\(\s*(?:!\s*{vp}|{vp}\s*(?:==\s*(?:NULL|0)|<\s*0))\s*\)'
)


def _callers_discharge(
    name: str, clean_lines: list[str], start_line: int, end_line: int
) -> bool:
    """Do all in-file callers of `name` convert its failure into an exception?

    The dominant false positive for `alloc_null_no_memerror` is a thin static
    allocation helper that returns NULL and lets its caller raise
    `MemoryError` (`list_allocate_array` / `new_values` in CPython).  If every
    call site in this file discharges the obligation, the helper is fine.
    """
    outside = "\n".join(
        clean_lines[:max(0, start_line - 1)] + clean_lines[end_line:]
    )
    call_re = re.compile(r'\b' + re.escape(name) + r'\s*\(')
    sites = 0
    for cm in call_re.finditer(outside):
        head = outside[max(0, cm.start() - 120):cm.start()]
        am = re.search(r'(' + _LVALUE + r')\s*=\s*(?:\([^)]*\)\s*)?$', head)
        if am is not None:
            after = outside[cm.end():cm.end() + 400]
            vp = _lvalue_pattern(am.group(1))
            gm = re.search(_RESULT_GUARD_TEMPLATE.format(vp=vp), after)
            if gm is None:
                return False
            branch = _guarded_branch(after[gm.end():])
        else:
            # `if (helper(...) < 0) { ... }` — the call is the condition.
            om = re.search(r'\bif\s*\(([!(\s]*)$', head)
            if om is None:
                return False
            open_idx = cm.start() - (len(head) - om.start(1))
            close = _match_paren(outside, open_idx - 1)
            if close is None:
                return False
            branch = _guarded_branch(outside[close + 1:close + 401])
        if not (_PYERR_RE.search(branch) or _GOTO_RE.search(branch)):
            return False
        sites += 1
    return sites > 0


def _match_paren(text: str, open_idx: int) -> int | None:
    """Index of the `)` matching the `(` at `open_idx`, or None."""
    if open_idx < 0 or open_idx >= len(text) or text[open_idx] != '(':
        return None
    depth = 0
    for k in range(open_idx, min(len(text), open_idx + 2000)):
        if text[k] == '(':
            depth += 1
        elif text[k] == ')':
            depth -= 1
            if depth == 0:
                return k
    return None


def _guarded_branch(text: str) -> str:
    """Return the body of the branch starting at `text` (post-`if (...)`)."""
    stripped = text.lstrip()
    pad = len(text) - len(stripped)
    if stripped.startswith('{'):
        depth = 0
        for k, ch in enumerate(text[pad:], start=pad):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[pad:k + 1]
        return text[pad:]
    end = text.find(';')
    return text[:end + 1] if end != -1 else text[:200]


def _check_unchecked_returns(clean: str) -> list[dict]:
    """Rules `missing_null_check` and `unchecked_return`."""
    findings: list[dict] = []
    for m in _ASSIGN_CALL_RE.finditer(clean):
        targets = _split_targets(m.group(1))
        if not targets:
            continue
        api = m.group(2)
        var = targets[-1]
        line_offset = clean[:m.start()].count('\n')
        # Skip past the call's own argument list; `f = PyMem_Malloc(sizeof
        # *f->a)` must not count its own arguments as a dereference.
        call_end = _match_paren(clean, m.end() - 1)
        after = clean[(call_end + 1) if call_end else m.end():]

        # FP class B: assignment inside a loop/if header is its own test.
        if _in_condition(clean, m.start()):
            continue

        aliases = _resolve_aliases(targets, after)
        patterns: list[str] = []
        for alias in aliases:
            patterns.extend(_null_check_patterns(_lvalue_pattern(alias)))
        check_m = _check_re(patterns).search(after)
        check_pos = check_m.start() if check_m else len(after)

        # A dereference *before* any test is the crash bug.
        vp = _lvalue_pattern(var)
        deref_m = re.search(
            vp + r'\s*->', after[:min(check_pos, _DEREF_WINDOW)]
        )
        if deref_m:
            findings.append({
                "type": "missing_null_check",
                "api_call": api,
                "variable": var,
                "line_offset": line_offset,
                "detail": (
                    f"Return value of {api} assigned to '{var}' "
                    f"is dereferenced without NULL check"
                ),
                "confidence": "high",
            })
            continue
        if check_m:
            continue
        # FP class A: the value flows straight back to the caller
        # (`return res;`, `return set_orig_class(obj, self);`), so the
        # callee's exception propagates untouched.  Not a bug.
        returned = _check_re([
            r'return\s+[^;]{0,200}?' + _lvalue_pattern(a) for a in aliases
        ])
        if returned.search(after):
            continue
        # Out-parameter store: the caller owns the NULL check by contract.
        if _is_out_param_assign(clean, m.start()):
            continue
        # Handed to a NULL-tolerant consumer.
        consumed = _check_re(
            [_NULL_TOLERANT_CONSUMER + _lvalue_pattern(a) for a in aliases]
        )
        if consumed.search(after):
            continue
        findings.append({
            "type": "unchecked_return",
            "api_call": api,
            "variable": var,
            "line_offset": line_offset,
            "detail": (
                f"Return value of {api} assigned to '{var}' "
                f"is neither checked for NULL nor returned unmodified"
            ),
            "confidence": "medium",
        })
    return findings


def _check_alloc_no_memerror(
    func: dict, clean: str, clean_lines: list[str] | None = None
) -> list[dict]:
    """Rule `alloc_null_no_memerror`.

    Fires only for allocators that do *not* set MemoryError themselves.  The
    old `return_null_no_exception` rule flagged every `return NULL` with no
    nearby `PyErr_Set*`, which cannot distinguish "forgot to raise" from
    "propagating the callee's exception"; this gate can.
    """
    findings: list[dict] = []
    for m in _ALLOC_CALL_RE.finditer(clean):
        targets = _split_targets(m.group(1))
        if not targets:
            continue
        api = m.group(2)
        var = targets[-1]
        call_end = _match_paren(clean, m.end() - 1)
        base = (call_end + 1) if call_end else m.end()
        after = clean[base:]
        vp = _lvalue_pattern(var)
        guard_re = re.compile(
            r'if\s*\(\s*(?:!\s*' + vp + r'|' + vp
            + r'\s*==\s*(?:NULL|0))\s*\)'
        )
        gm = guard_re.search(after)
        if gm is None:
            continue
        branch = _guarded_branch(after[gm.end():])
        if _PYERR_RE.search(branch):
            continue
        # A goto hands the obligation to a label we cannot follow cheaply.
        if _GOTO_RE.search(branch):
            continue
        if not _ERROR_RETURN_RE.search(branch):
            continue
        # A thin allocation helper may legitimately defer the MemoryError to
        # its callers; check them before reporting.
        if clean_lines is not None and _callers_discharge(
            func["name"], clean_lines, func["start_line"], func["end_line"]
        ):
            continue
        findings.append({
            "type": "alloc_null_no_memerror",
            "api_call": api,
            "variable": var,
            "line_offset": clean[:m.start()].count('\n'),
            "guard_line_offset": (clean[:base + gm.start()]).count('\n'),
            "detail": (
                f"{api} does not set MemoryError; the failure branch for "
                f"'{var}' returns an error sentinel without PyErr_NoMemory()"
            ),
            "confidence": "high",
        })
    return findings


_ANY_PYLONG_AS_RE = re.compile(r'\b_?PyLong_As\w*\s*\(')
_CHECKED_LONG_LOOKBACK = 6


def _cleared_call_is_checked_long(clean: str, window: str) -> bool:
    """Is every ``PyLong_As*`` in ``window`` applied to a checked long?

    Returns False when the window holds no ``PyLong_As*`` at all, so the gate
    only ever suppresses the specific measured class.
    """
    found = False
    for m in _ANY_PYLONG_AS_RE.finditer(window):
        close = _match_paren(window, m.end() - 1)
        if close is None:
            return False
        found = True
        if not _pylong_checked(clean, window[m.end():close]):
            return False
    return found


def _check_unconditional_pyerr_clear(func: dict, clean: str) -> list[dict]:
    """Rule `unconditional_pyerr_clear`.

    A `PyErr_Clear()` that is not narrowed by `PyErr_ExceptionMatches` (or a
    save/restore call) discards *whatever* exception is live — including
    KeyboardInterrupt and MemoryError raised by user code called a few lines
    earlier.  See Objects/unionobject.c:172.
    """
    if _DESTRUCTOR_RE.search(func["name"]):
        return []
    findings: list[dict] = []
    lines = clean.split('\n')
    for idx, line in enumerate(lines):
        if not _PYERR_CLEAR_RE.search(line):
            continue
        window = "\n".join(lines[max(0, idx - 3):idx + 1])
        if _PYERR_CLEAR_GUARD_RE.search(window):
            continue
        # Only report clears that sit inside a failure branch: that is the
        # shape where a user-supplied exception can be the thing discarded.
        if not _FAILURE_TEST_RE.search(window):
            continue
        # Dominant residual FP class after dropping `Occurred` from the guard
        # alternation (6 of 9 dismissals in the tree-wide hand-check): the
        # failure being cleared is a `PyLong_As*` on an operand the function
        # already `PyLong_Check`-ed. Those converters short-circuit on
        # PyLong_Check, so not even an int subclass runs user code — only an
        # OverflowError can be pending, and it is always the case the branch
        # is handling. (rangeobject.c range_iter, _sqlite/blob.c
        # ass_subscript_index, itertoolsmodule.c count_repr, pythonrun.c
        # parse_exit_code.)
        # A wider lookback than the guard window: the conversion and its `== -1`
        # test are often separated by an explanatory comment (Python/pythonrun.c
        # parse_exit_code puts two between them).
        if _cleared_call_is_checked_long(
            clean, "\n".join(lines[max(0, idx - _CHECKED_LONG_LOOKBACK):idx + 1])
        ):
            continue
        findings.append({
            "type": "unconditional_pyerr_clear",
            "api_call": "PyErr_Clear",
            "line_offset": idx,
            "detail": (
                "PyErr_Clear() with no PyErr_ExceptionMatches within the "
                "preceding 3 lines — swallows any live exception, including "
                "KeyboardInterrupt and MemoryError raised by user code"
            ),
            "confidence": "medium",
        })
    return findings


# ---------------------------------------------------------------------------
# PyLong_As* sentinel rule
# ---------------------------------------------------------------------------
#
# These converters return -1 both as their error sentinel *and* as the honest
# conversion of the integer -1, so `if (x == -1) return -1;` turns a perfectly
# ordinary value into a silent failure: the function returns its error sentinel
# with no exception set, and the caller propagates NULL to Python, where
# _Py_CheckFunctionResult aborts a debug build and raises SystemError on a
# release build.  The correct spelling is `if (x == -1 && PyErr_Occurred())`.
#
# Measured on Modules/ + Objects/ + Python/ at 3.16.0a0: 4 candidates,
# 4 true positives, 0 false positives — all four in Modules/_zoneinfo.c, whose
# own neighbouring lines (:1063, :2304) show the guarded twin.

PYLONG_SENTINEL_APIS = frozenset({
    "PyLong_AsLong", "PyLong_AsLongLong", "PyLong_AsSsize_t",
    "PyLong_AsInt", "PyLong_AsSize_t",
    "PyLong_AsUnsignedLong", "PyLong_AsUnsignedLongLong",
})

# An lvalue that can be subscripted: `self->trans_list_utc[i]`.
_SENTINEL_LVALUE = r'\w+(?:\[[^\]\n]*\]|\s*->\s*\w+|\s*\.\s*\w+)*'

_PYLONG_SENTINEL_RE = re.compile(
    r'(' + _SENTINEL_LVALUE + r')\s*=\s*' + _CAST
    + r'(' + _alt(PYLONG_SENTINEL_APIS) + r')\s*\('
)

# `== -1`, `== (unsigned long)-1`, `== -1L` — but not `== -10`.  Matched
# against a whitespace-free rendering of the line, so `a -> b [i]` and
# `a->b[i]` compare equal.
_MINUS_ONE = r'==(?:\([\w*]+\))?-1(?![0-9])'

# `PyErr_Occurred()` *is* the right narrowing for this rule: it is what
# separates the sentinel from the honest -1.
_SENTINEL_NARROWING_RE = re.compile(
    r'\b_?PyErr_(?:Occurred|ExceptionMatches|GivenExceptionMatches)\s*\('
)

# How many lines after the conversion to look for its `== -1` test.
_SENTINEL_WINDOW = 6


def _pylong_checked(clean: str, operand: str) -> bool:
    """Is the converter's operand already `PyLong_Check`-ed in this function?

    Measured FP class.  With a checked long no user code runs inside the
    converter, so the only thing it can raise is an OverflowError the caller
    can re-derive from the magnitude — and CPython uses that shape for
    *non-error* sentinels: `Python/flowgraph.c` `const_folding_safe_power` /
    `const_folding_safe_lshift` return NULL to mean "not foldable", and
    `fold_const_binop:1957` narrows to KeyboardInterrupt and clears the rest.
    Both are the only cast-form (`== (size_t)-1`) hits tree-wide.
    """
    operand = _normalize(operand)
    if not re.fullmatch(r'\w+(?:(?:->|\.)\w+)*', operand):
        return False
    return re.search(
        r'\bPyLong_Check(?:Exact)?\(' + re.escape(operand) + r'\)',
        _normalize(clean),
    ) is not None


def _check_pylong_sentinel(clean: str) -> list[dict]:
    """Rule ``pylong_sentinel_no_errcheck``."""
    findings: list[dict] = []
    lines = clean.split('\n')
    for m in _PYLONG_SENTINEL_RE.finditer(clean):
        var, api = m.group(1), m.group(2)
        call_end = _match_paren(clean, m.end() - 1)
        operand = clean[m.end():call_end] if call_end else ""
        if _pylong_checked(clean, operand):
            continue
        assign_idx = clean[:m.start()].count('\n')
        cmp_re = re.compile(re.escape(_normalize(var)) + _MINUS_ONE)
        guard_idx = None
        for j in range(assign_idx + 1,
                       min(len(lines), assign_idx + 1 + _SENTINEL_WINDOW)):
            if cmp_re.search(_normalize(lines[j])):
                guard_idx = j
                break
        if guard_idx is None:
            continue
        # The narrowing may sit on the guard line itself (`&& PyErr_Occurred()`)
        # or on the line after it (a multi-line condition).
        if _SENTINEL_NARROWING_RE.search(
            " ".join(lines[guard_idx:guard_idx + 2])
        ):
            continue
        line_start = sum(len(ln) + 1 for ln in lines[:guard_idx])
        branch = ""
        om = re.search(r'\bif\s*\(', clean[line_start:line_start + 400])
        if om is not None:
            close = _match_paren(clean, line_start + om.end() - 1)
            if close is not None:
                branch = _guarded_branch(clean[close + 1:close + 401])
        errors_out = bool(
            _ERROR_RETURN_RE.search(branch) or _GOTO_RE.search(branch)
        )
        findings.append({
            "type": "pylong_sentinel_no_errcheck",
            "api_call": api,
            "variable": var.strip(),
            "line_offset": guard_idx,
            "assign_line_offset": assign_idx,
            "detail": (
                f"'{var.strip()}' comes from {api}, whose -1 return is both the "
                f"error sentinel and the honest conversion of the integer -1, "
                f"but the guard tests only '== -1' with no PyErr_Occurred(). "
                f"An input whose value really is -1 takes the failure branch "
                f"with no exception set"
                + (" and returns the function's error sentinel"
                   if errors_out else "")
                + " — the caller propagates NULL to Python and "
                "_Py_CheckFunctionResult raises SystemError (SIGABRT on a "
                "debug build). Fix: 'if (x == -1 && PyErr_Occurred())'."
            ),
            "confidence": "high" if errors_out else "medium",
        })
    return findings


def _check_unchecked_parse(clean: str) -> list[dict]:
    """Rule `unchecked_parse`."""
    findings: list[dict] = []
    for m in _PYARG_PARSE_RE.finditer(clean):
        var = m.group(1)
        line_offset = clean[:m.start()].count('\n')
        after = clean[m.end():m.end() + 300]
        vp = _lvalue_pattern(var)
        if _check_re(_null_check_patterns(vp) + [vp + r'\s*<\s*0']).search(after):
            continue
        findings.append({
            "type": "unchecked_parse",
            "api_call": "PyArg_ParseTuple",
            "variable": var,
            "line_offset": line_offset,
            "detail": (
                "PyArg_ParseTuple return value not checked — "
                "extracted arguments may be uninitialized on failure"
            ),
            "confidence": "medium",
        })
    return findings


def analyze_function_errors(
    func: dict, clean_lines: list[str] | None = None
) -> list[dict]:
    """Analyze error handling in a single function.

    `clean_lines` is the comment-stripped enclosing file split into lines; it
    enables the intra-file caller check used by `alloc_null_no_memerror`.
    """
    clean = strip_comments_and_strings(func["body"])
    findings: list[dict] = []
    findings.extend(_check_unchecked_returns(clean))
    findings.extend(_check_alloc_no_memerror(func, clean, clean_lines))
    findings.extend(_check_unconditional_pyerr_clear(func, clean))
    findings.extend(_check_pylong_sentinel(clean))
    findings.extend(_check_unchecked_parse(clean))
    return findings


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(target: str, *, max_files: int = 0) -> dict:
    """Analyze error handling patterns for the given target path."""
    target_path = Path(target).resolve()
    project_root = find_cpython_root(target_path)
    if project_root is None:
        project_root = target_path if target_path.is_dir() else target_path.parent
    # discover_c_files() handles a single file itself; passing the parent
    # here would silently widen a single-file request to its whole directory.
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

        rel = str(filepath.relative_to(project_root))
        functions = find_functions(source)
        clean_lines = strip_comments_and_strings(source).split('\n')
        for func in functions:
            functions_analyzed += 1
            func_findings = analyze_function_errors(func, clean_lines)
            for finding in func_findings:
                finding["file"] = rel
                finding["function"] = func["name"]
                # line_offset is 0-based within the function body, and
                # body_start_line is the 1-based line of the body's first line.
                finding["line"] = (
                    func["body_start_line"] + finding.pop("line_offset")
                )
                if "guard_line_offset" in finding:
                    finding["guard_line"] = (
                        func["body_start_line"]
                        + finding.pop("guard_line_offset")
                    )
                if "assign_line_offset" in finding:
                    finding["assign_line"] = (
                        func["body_start_line"]
                        + finding.pop("assign_line_offset")
                    )
                all_findings.append(finding)

    def count(kind: str) -> int:
        return sum(1 for f in all_findings if f["type"] == kind)

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_analyzed": files_analyzed,
        "functions_analyzed": functions_analyzed,
        "findings": all_findings,
        "summary": {
            "missing_null_checks": count("missing_null_check"),
            "unchecked_returns": count("unchecked_return"),
            "alloc_null_no_memerror": count("alloc_null_no_memerror"),
            "unconditional_pyerr_clear": count("unconditional_pyerr_clear"),
            "pylong_sentinel_no_errcheck": count("pylong_sentinel_no_errcheck"),
            "unchecked_parse_calls": count("unchecked_parse"),
            "total_findings": len(all_findings),
        },
    }


def main() -> None:
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

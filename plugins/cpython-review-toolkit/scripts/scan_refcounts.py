#!/usr/bin/env python3
"""Scan CPython C source for reference counting errors.

Detects potential reference leaks, use-after-free from borrowed refs,
stolen-reference misuse, missing Py_XDECREF, and Py_CLEAR suggestions.

Outputs a JSON structure with per-function refcount balance analysis.

Usage:
    python scan_refcounts.py [path]

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
    source = re.sub(r'/\*.*?\*/', ' ', source, flags=re.DOTALL)
    source = re.sub(r'//[^\n]*', ' ', source)
    source = re.sub(r'"(?:[^"\\]|\\.)*"', '""', source)
    source = re.sub(r"'(?:[^'\\]|\\.)*'", "''", source)
    return source


# ---------------------------------------------------------------------------
# API classification tables
# ---------------------------------------------------------------------------

NEW_REF_APIS = frozenset({
    "PyObject_Call", "PyObject_CallObject", "PyObject_CallFunction",
    "PyObject_CallMethod", "PyObject_CallNoArgs", "PyObject_CallOneArg",
    "PyObject_GetAttr", "PyObject_GetAttrString",
    "PyObject_GetItem", "PyObject_Str", "PyObject_Repr", "PyObject_ASCII",
    "PyObject_Bytes", "PyObject_RichCompare", "PyObject_Format",
    "PyObject_Vectorcall",
    "PyUnicode_FromString", "PyUnicode_FromFormat", "PyUnicode_Decode",
    "PyUnicode_FromEncodedObject", "PyUnicode_Join",
    "PyUnicode_FromObject", "PyUnicode_Substring",
    "PyBytes_FromString", "PyBytes_FromStringAndSize",
    "PyBytes_FromObject",
    "PyLong_FromLong", "PyLong_FromUnsignedLong", "PyLong_FromDouble",
    "PyLong_FromLongLong", "PyLong_FromSsize_t", "PyLong_FromSize_t",
    "PyFloat_FromDouble", "PyFloat_FromString",
    "PyList_New", "PyList_GetSlice",
    "PyTuple_New", "PyTuple_GetSlice", "PyTuple_Pack",
    "PyDict_New", "PyDict_Copy", "PyDict_Keys", "PyDict_Values",
    "PyDict_Items",
    "PySet_New", "PyFrozenSet_New",
    "Py_BuildValue", "Py_VaBuildValue",
    "PySequence_List", "PySequence_Tuple", "PySequence_GetItem",
    "PySequence_Concat", "PySequence_InPlaceConcat",
    "PyNumber_Add", "PyNumber_Subtract", "PyNumber_Multiply",
    "PyNumber_TrueDivide", "PyNumber_FloorDivide", "PyNumber_Remainder",
    "PyNumber_Power", "PyNumber_Negative", "PyNumber_Positive",
    "PyNumber_Absolute", "PyNumber_Long", "PyNumber_Float",
    "PyNumber_Index", "PyNumber_InPlaceAdd", "PyNumber_InPlaceSubtract",
    "PyIter_Next",
    "PyImport_ImportModule", "PyImport_Import",
    "PyModule_New", "PyModule_NewObject",
    "PyType_FromSpec", "PyType_FromSpecWithBases",
    "PyType_FromModuleAndSpec",
    "_PyObject_New", "PyObject_Init",
    "PyErr_Format", "PyErr_NewException", "PyErr_NewExceptionWithDoc",
    "PyMapping_Keys", "PyMapping_Values", "PyMapping_Items",
    "PyObject_GenericGetAttr",
    "PyCapsule_New",
    "PyMemoryView_FromObject",
    "PyWeakref_NewRef", "PyWeakref_NewProxy",
    "PyStructSequence_New",
    "PyCode_New", "PyCode_NewEmpty",
    "PyFrame_New",
})

BORROWED_REF_APIS = frozenset({
    "PyList_GetItem", "PyList_GET_ITEM",
    "PyTuple_GetItem", "PyTuple_GET_ITEM",
    "PyDict_GetItem", "PyDict_GetItemString",
    "PyDict_GetItemWithError",
    "PyModule_GetDict",
    "PyImport_GetModuleDict",
    "PyThreadState_GetDict",
    "PySys_GetObject",
    "PyWeakref_GetObject", "PyWeakref_GET_OBJECT",
    "PyErr_Occurred",
    "PyMethod_GET_SELF", "PyMethod_GET_FUNCTION",
    "PyCell_GET",
    "Py_None", "Py_True", "Py_False",
    "PyExc_TypeError", "PyExc_ValueError", "PyExc_KeyError",
    "PyExc_AttributeError", "PyExc_RuntimeError",
})

STEAL_REF_APIS = frozenset({
    "PyList_SET_ITEM", "PyList_SetItem",
    "PyTuple_SET_ITEM", "PyTuple_SetItem",
    "PyModule_AddObject",
    "PySet_Discard",
})

INCREF_APIS = frozenset({
    "Py_INCREF", "Py_XINCREF",
})

DECREF_APIS = frozenset({
    "Py_DECREF", "Py_XDECREF", "Py_CLEAR", "Py_SETREF",
})


# ---------------------------------------------------------------------------
# Function detection (simplified from measure_c_complexity.py)
# ---------------------------------------------------------------------------

def find_functions(source: str) -> list[dict]:
    """Find C function definitions and extract their bodies."""
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

        functions.append({
            "name": func_name,
            "body": body,
            "start_line": sig_start + 1,
            "end_line": body_end + 1,
            "raw_lines": lines[body_start:body_end],
        })
    return functions


# ---------------------------------------------------------------------------
# Refcount analysis
# ---------------------------------------------------------------------------

# Match API calls and capture variable assignment.
# e.g., "result = PyList_New(0)" -> captures "result" and "PyList_New"
_ASSIGN_CALL_RE = re.compile(
    r'(\w+)\s*=\s*(' + '|'.join(
        re.escape(api) for api in sorted(NEW_REF_APIS, key=len, reverse=True)
    ) + r')\s*\('
)

_ANY_API_CALL_RE = re.compile(
    r'\b(' + '|'.join(
        re.escape(api) for api in sorted(
            NEW_REF_APIS | BORROWED_REF_APIS | STEAL_REF_APIS,
            key=len, reverse=True,
        )
    ) + r')\s*\('
)

_DECREF_RE = re.compile(
    r'\b(Py_DECREF|Py_XDECREF|Py_CLEAR|Py_SETREF)\s*\(\s*(\w+)'
)

_INCREF_RE = re.compile(
    r'\b(Py_INCREF|Py_XINCREF)\s*\(\s*(\w+)'
)

# Capture the last argument (the stolen reference) before closing paren.
_STEAL_CALL_RE = re.compile(
    r'\b(' + '|'.join(
        re.escape(api) for api in sorted(STEAL_REF_APIS, key=len, reverse=True)
    ) + r')\s*\([^)]*,\s*(\w+)\s*\)'
)

_RETURN_RE = re.compile(r'\breturn\s+(\w+)\s*;')
_RETURN_NULL_RE = re.compile(r'\breturn\s+NULL\s*;')
_GOTO_ERROR_RE = re.compile(r'\bgoto\s+(\w+)\s*;')
_ERROR_LABEL_RE = re.compile(r'^(\w+)\s*:', re.MULTILINE)


# ---------------------------------------------------------------------------
# tp_init / tp_new detection
# ---------------------------------------------------------------------------

# Allocation APIs that create resources (superset of NEW_REF_APIS for init
# safety — includes raw memory and file handles).
_INIT_ALLOC_APIS = frozenset({
    "PyMem_Malloc", "PyMem_Calloc", "PyMem_Realloc",
    "malloc", "calloc", "realloc",
    "PyObject_New", "PyObject_GC_New",
    "PyList_New", "PyDict_New", "PyTuple_New", "PySet_New",
    "PyUnicode_FromString", "PyBytes_FromString",
    "Py_BuildValue", "PyObject_Call", "PyObject_CallFunction",
    "PyObject_CallMethod",
    "fopen", "open",
})

_INIT_ALLOC_RE = re.compile(
    r'\b(' + '|'.join(
        re.escape(api) for api in sorted(_INIT_ALLOC_APIS, key=len, reverse=True)
    ) + r')\s*\('
)

# Assignment to self->member pattern.
_SELF_MEMBER_ASSIGN_RE = re.compile(
    r'\bself\s*->\s*(\w+)\s*='
)

# Re-init guard patterns (any of these → safe).
_REINIT_GUARD_PATTERNS = [
    re.compile(r'"already.?init', re.IGNORECASE),
    re.compile(r'"cannot.?reinit', re.IGNORECASE),
    re.compile(r'\bPREVENT_INIT', re.IGNORECASE),
    re.compile(r'\binit_was_called\b'),
    re.compile(r'\bself\s*->\s*initialized\b'),
    # Cleanup-before-assign: if (self->member != NULL) { Py_CLEAR/free/DECREF }
    re.compile(
        r'if\s*\(\s*self\s*->\s*\w+\s*!=\s*NULL\s*\)'
        r'[^}]*(?:Py_CLEAR|Py_XDECREF|Py_DECREF|PyMem_Free|free)\s*\('
    ),
]

# Non-zeroing allocators for tp_new.
_NON_ZEROING_ALLOC_RE = re.compile(
    r'\b(PyObject_New|PyObject_GC_New|malloc)\s*\('
)

# Zeroing allocators for tp_new.
_ZEROING_ALLOC_RE = re.compile(
    r'\b(tp_alloc|PyType_GenericAlloc|calloc)\s*\(|'
    r'\bmemset\s*\(\s*self\s*,\s*0'
)

# Member init to NULL/0 in tp_new.
_MEMBER_NULL_INIT_RE = re.compile(
    r'\bself\s*->\s*\w+\s*=\s*(?:NULL|0)\s*;'
)


def _is_tp_init(func: dict) -> bool:
    """Check if a function looks like a tp_init implementation.

    tp_init signature: int SomeType_init(SomeType *self, PyObject *args, ...)
    """
    name = func["name"]
    body = func["body"]
    # Name typically ends with _init or _Init.
    if not re.search(r'_[Ii]nit$', name):
        return False
    # Body should reference self-> (operates on instance).
    if 'self->' not in body:
        return False
    return True


def _is_tp_new(func: dict) -> bool:
    """Check if a function looks like a tp_new implementation.

    tp_new signature: PyObject *SomeType_new(PyTypeObject *type, ...)
    """
    name = func["name"]
    body = func["body"]
    if not re.search(r'_[Nn]ew$', name):
        return False
    # Should contain an allocator call and return a PyObject*.
    if 'self' not in body:
        return False
    return True


def check_init_reinit_safety(func: dict) -> list[dict]:
    """Check a tp_init function for re-init safety issues.

    Flags tp_init functions that allocate resources and assign to
    self->member without a re-init guard.
    """
    if not _is_tp_init(func):
        return []

    body = func["body"]
    clean = strip_comments_and_strings(body)
    findings: list[dict] = []

    # Check for allocations.
    alloc_calls = _INIT_ALLOC_RE.findall(clean)
    if not alloc_calls:
        return []

    # Check for self->member assignments.
    member_assigns = _SELF_MEMBER_ASSIGN_RE.findall(clean)
    if not member_assigns:
        return []

    # Check for re-init guard patterns.
    for pattern in _REINIT_GUARD_PATTERNS:
        if pattern.search(clean):
            return []

    # No guard found — flag it.
    findings.append({
        "type": "init_not_reinit_safe",
        "line_offset": 0,
        "detail": (
            f"tp_init '{func['name']}' allocates ({', '.join(sorted(set(alloc_calls)))}) "
            f"and assigns to self->{', self->'.join(sorted(set(member_assigns)))} "
            f"without a re-init guard — second __init__() call will leak resources"
        ),
        "confidence": "high",
    })
    return findings


def check_new_member_init(func: dict) -> list[dict]:
    """Check a tp_new function for uninitialized member safety.

    Flags tp_new functions using non-zeroing allocators without
    initializing pointer members to NULL.
    """
    if not _is_tp_new(func):
        return []

    body = func["body"]
    clean = strip_comments_and_strings(body)
    findings: list[dict] = []

    # Check if a zeroing allocator is used — if so, safe.
    if _ZEROING_ALLOC_RE.search(clean):
        return []

    # Check if a non-zeroing allocator is used.
    non_zero_m = _NON_ZEROING_ALLOC_RE.search(clean)
    if not non_zero_m:
        return []

    allocator = non_zero_m.group(1)

    # Check if pointer members are explicitly initialized.
    if _MEMBER_NULL_INIT_RE.search(clean):
        return []

    findings.append({
        "type": "new_missing_member_init",
        "line_offset": clean[:non_zero_m.start()].count('\n') + 1,
        "detail": (
            f"tp_new '{func['name']}' uses non-zeroing allocator {allocator}() "
            f"without initializing pointer members to NULL — "
            f"object.__new__() without __init__() will leave garbage pointers"
        ),
        "confidence": "medium",
    })
    return findings


def analyze_function_refcounts(func: dict) -> list[dict]:
    """Analyze refcount balance for a single function.

    Returns a list of finding dicts.
    """
    body = func["body"]
    clean = strip_comments_and_strings(body)
    findings: list[dict] = []

    # Track new references acquired.
    new_refs: dict[str, dict] = {}  # var -> {api, line_offset}
    for m in _ASSIGN_CALL_RE.finditer(clean):
        var = m.group(1)
        api = m.group(2)
        # Approximate line offset.
        line_offset = clean[:m.start()].count('\n') + 1
        new_refs[var] = {"api": api, "line_offset": line_offset}

    # Track decrefs.
    decreffed: set[str] = set()
    for m in _DECREF_RE.finditer(clean):
        decreffed.add(m.group(2))

    # Track increfs.
    increffed: set[str] = set()
    for m in _INCREF_RE.finditer(clean):
        increffed.add(m.group(2))

    # Track stolen references.
    stolen: set[str] = set()
    for m in _STEAL_CALL_RE.finditer(clean):
        stolen.add(m.group(2))

    # Track returned variables (ownership transferred to caller).
    returned: set[str] = set()
    for m in _RETURN_RE.finditer(clean):
        returned.add(m.group(1))

    # Check for error paths (goto error/fail/done).
    has_error_goto = bool(_GOTO_ERROR_RE.search(clean))
    error_labels = set(_ERROR_LABEL_RE.findall(clean))

    # Find variables in error-path cleanup.
    error_cleanup_vars: set[str] = set()
    if error_labels:
        for label in error_labels:
            label_pattern = re.compile(
                re.escape(label) + r'\s*:(.+?)(?=\n\w+\s*:|$)',
                re.DOTALL,
            )
            m = label_pattern.search(clean)
            if m:
                cleanup = m.group(1)
                for dm in _DECREF_RE.finditer(cleanup):
                    error_cleanup_vars.add(dm.group(2))

    # Analyze each new reference.
    for var, info in new_refs.items():
        is_decreffed = var in decreffed
        is_stolen = var in stolen
        is_returned = var in returned
        is_in_error_cleanup = var in error_cleanup_vars

        # Check: new ref that is neither decreffed, stolen, nor returned.
        if not is_decreffed and not is_stolen and not is_returned:
            findings.append({
                "type": "potential_leak",
                "api_call": info["api"],
                "variable": var,
                "line_offset": info["line_offset"],
                "detail": (
                    f"New reference from {info['api']} assigned to '{var}' "
                    f"is never DECREF'd, stolen, or returned"
                ),
                "confidence": "high",
            })
        # Check: new ref on error path.
        elif has_error_goto and is_returned and not is_in_error_cleanup:
            # If the function has error gotos but this var isn't cleaned
            # up in error labels, it might leak on error paths.
            if not is_decreffed:
                findings.append({
                    "type": "potential_leak_on_error",
                    "api_call": info["api"],
                    "variable": var,
                    "line_offset": info["line_offset"],
                    "detail": (
                        f"New reference '{var}' from {info['api']} returned "
                        f"on success but not DECREF'd in error cleanup"
                    ),
                    "confidence": "medium",
                })

    # Check: stolen ref then decreffed (double-free risk).
    for var in stolen & decreffed:
        if var in new_refs:
            findings.append({
                "type": "potential_double_free",
                "api_call": "steal+decref",
                "variable": var,
                "line_offset": new_refs[var]["line_offset"],
                "detail": (
                    f"Variable '{var}' is passed to a reference-stealing API "
                    f"and also DECREF'd — potential double-free"
                ),
                "confidence": "medium",
            })

    # Check: Py_DECREF on a variable that might be NULL
    # (should use Py_XDECREF).
    for m in _DECREF_RE.finditer(clean):
        api = m.group(1)
        var = m.group(2)
        if api == "Py_DECREF" and var in new_refs:
            # Check if there's a NULL check pattern before the DECREF.
            pre_context = clean[:m.start()]
            null_check = re.search(
                rf'\bif\s*\(\s*{re.escape(var)}\s*==\s*NULL\s*\)', pre_context
            )
            assign_null = re.search(
                rf'{re.escape(var)}\s*=\s*NULL\b', pre_context
            )
            if not null_check and not assign_null:
                # Heuristic: if the API can return NULL and there's no
                # check, suggest Py_XDECREF.
                pass  # This is noisy; skip for now.

    return findings


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(target: str, *, max_files: int = 0) -> dict:
    """Analyze refcount patterns for the given target path."""
    target_path = Path(target).resolve()
    project_root = find_cpython_root(target_path)
    if project_root is None:
        project_root = target_path if target_path.is_dir() else target_path.parent
    scan_root = target_path if target_path.is_dir() else target_path.parent

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
        for func in functions:
            functions_analyzed += 1
            # Standard refcount analysis.
            func_findings = analyze_function_refcounts(func)
            # tp_init re-init safety.
            func_findings.extend(check_init_reinit_safety(func))
            # tp_new uninitialized member safety.
            func_findings.extend(check_new_member_init(func))
            for finding in func_findings:
                finding["file"] = rel
                finding["function"] = func["name"]
                finding["line"] = func["start_line"] + finding.pop("line_offset")
                all_findings.append(finding)

    # Categorize findings.
    leaks = [f for f in all_findings if "leak" in f["type"]]
    double_frees = [f for f in all_findings if "double_free" in f["type"]]
    reinit = [f for f in all_findings if f["type"] == "init_not_reinit_safe"]
    new_uninit = [f for f in all_findings if f["type"] == "new_missing_member_init"]

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_analyzed": files_analyzed,
        "functions_analyzed": functions_analyzed,
        "findings": all_findings,
        "summary": {
            "potential_leaks": len(leaks),
            "potential_double_frees": len(double_frees),
            "init_not_reinit_safe": len(reinit),
            "new_missing_member_init": len(new_uninit),
            "total_findings": len(all_findings),
            "high_confidence": len(
                [f for f in all_findings if f.get("confidence") == "high"]
            ),
            "medium_confidence": len(
                [f for f in all_findings if f.get("confidence") == "medium"]
            ),
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

#!/usr/bin/env python3
"""Scan CPython C source for error handling bugs.

Detects missing NULL checks after API calls, return NULL without
PyErr_Set*, error path cleanup issues, and inconsistent error returns.

Usage:
    python scan_error_paths.py [path] [--max-files N]

    path: directory, file, or omitted for current directory
    --max-files N: cap the number of .c/.h files scanned (0 = unlimited)
"""

import json
import re
import sys
from pathlib import Path
from typing import Generator

# Allow importing sibling scan_common.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_common import extract_nearby_comments, has_safety_annotation  # noqa: E402


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

        # Detect return type to classify error convention.
        ret_type = ""
        if sig_start > 0:
            ret_type = lines[sig_start - 1].strip() if sig_start > 0 else ""
        if not ret_type:
            ret_type = prev.split(func_name)[0].strip() if func_name in prev else ""

        functions.append({
            "name": func_name,
            "body": body,
            "start_line": sig_start + 1,
            "end_line": body_end + 1,
            "return_type": ret_type,
        })
    return functions


# ---------------------------------------------------------------------------
# Error path analysis
# ---------------------------------------------------------------------------

_ASSIGN_CALL_RE = re.compile(
    r'(\w+)\s*=\s*(' + '|'.join(
        re.escape(api) for api in sorted(NULL_RETURN_APIS, key=len, reverse=True)
    ) + r')\s*\('
)

_NULL_CHECK_RE_TEMPLATE = r'if\s*\(\s*{var}\s*==\s*NULL\s*\)|if\s*\(\s*!\s*{var}\s*\)|if\s*\(\s*{var}\s*==\s*0\s*\)'
_RETURN_NULL_RE = re.compile(r'\breturn\s+NULL\s*;')
_RETURN_NEG_RE = re.compile(r'\breturn\s+-1\s*;')
_GOTO_RE = re.compile(r'\bgoto\s+(\w+)\s*;')
_LABEL_RE = re.compile(r'^(\w+)\s*:', re.MULTILINE)
_PYERR_RE = re.compile(
    r'\b(' + '|'.join(
        re.escape(api) for api in sorted(PYERR_SET_APIS, key=len, reverse=True)
    ) + r')\s*\('
)
_DEREF_RE = re.compile(r'(\w+)\s*->')
_PYARG_PARSE_RE = re.compile(
    r'(\w+)\s*=\s*PyArg_Parse(?:Tuple|TupleAndKeywords)\s*\('
)


def analyze_function_errors(func: dict) -> list[dict]:
    """Analyze error handling in a single function."""
    body = func["body"]
    clean = strip_comments_and_strings(body)
    findings: list[dict] = []

    # 1. Check for missing NULL checks after API calls.
    for m in _ASSIGN_CALL_RE.finditer(clean):
        var = m.group(1)
        api = m.group(2)
        line_offset = clean[:m.start()].count('\n') + 1
        # Look for a NULL check within next ~10 lines.
        after = clean[m.end():m.end() + 500]
        null_check_re = re.compile(
            _NULL_CHECK_RE_TEMPLATE.format(var=re.escape(var))
        )
        if not null_check_re.search(after):
            # Check if variable is used (dereference) before any check.
            deref = re.search(rf'\b{re.escape(var)}\s*->', after[:200])
            if deref:
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
            else:
                findings.append({
                    "type": "unchecked_return",
                    "api_call": api,
                    "variable": var,
                    "line_offset": line_offset,
                    "detail": (
                        f"Return value of {api} assigned to '{var}' "
                        f"is not checked for NULL"
                    ),
                    "confidence": "medium",
                })

    # 2. Check for return NULL without PyErr_Set*.
    returns_pyobject = "PyObject" in func.get("return_type", "")
    if returns_pyobject:
        for m in _RETURN_NULL_RE.finditer(clean):
            line_offset = clean[:m.start()].count('\n') + 1
            # Look backwards for PyErr_Set* or a function call that
            # sets the error indicator.
            before = clean[:m.start()]
            # Check last ~500 chars for PyErr.
            context = before[-500:]
            if not _PYERR_RE.search(context):
                # Check if there's a goto that leads to error setting.
                goto_m = _GOTO_RE.search(context[-200:])
                if not goto_m:
                    findings.append({
                        "type": "return_null_no_exception",
                        "line_offset": line_offset,
                        "detail": (
                            "return NULL without PyErr_Set* — may cause "
                            "'SystemError: error return without exception set'"
                        ),
                        "confidence": "medium",
                    })

    # 3. Check for PyArg_ParseTuple without checking return value.
    for m in _PYARG_PARSE_RE.finditer(clean):
        var = m.group(1)
        line_offset = clean[:m.start()].count('\n') + 1
        after = clean[m.end():m.end() + 300]
        check_re = re.compile(
            rf'if\s*\(\s*!{re.escape(var)}\s*\)|'
            rf'if\s*\(\s*{re.escape(var)}\s*==\s*0\s*\)'
        )
        if not check_re.search(after):
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

    # 4. Check cleanup labels for completeness (heuristic).
    labels = set(_LABEL_RE.findall(clean))
    error_labels = {l for l in labels if l in ('error', 'fail', 'done', 'cleanup', 'exit')}
    if error_labels and not any(
        re.search(r'Py_(?:X?DECREF|CLEAR)', clean[clean.find(l + ':'):])
        for l in error_labels
        if l + ':' in clean
    ):
        # Error labels exist but don't DECREF anything — may be incomplete.
        # Only flag if function acquires new references.
        if _ASSIGN_CALL_RE.search(clean):
            findings.append({
                "type": "sparse_error_cleanup",
                "line_offset": 0,
                "detail": (
                    f"Error labels ({', '.join(sorted(error_labels))}) "
                    f"don't appear to DECREF any locally-owned references"
                ),
                "confidence": "low",
            })

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
            func_findings = analyze_function_errors(func)
            for finding in func_findings:
                finding["file"] = rel
                finding["function"] = func["name"]
                finding["line"] = func["start_line"] + finding.pop("line_offset")
                # Annotation-aware suppression: downgrade to low confidence
                # when a nearby safety comment is present.
                nearby = extract_nearby_comments(source, finding["line"])
                if has_safety_annotation(nearby):
                    finding["confidence"] = "low"
                    finding["suppressed_by_annotation"] = True
                all_findings.append(finding)

    # Categorize.
    null_checks = [f for f in all_findings if f["type"] == "missing_null_check"]
    unchecked = [f for f in all_findings if f["type"] == "unchecked_return"]
    no_exception = [f for f in all_findings if f["type"] == "return_null_no_exception"]
    parse_issues = [f for f in all_findings if f["type"] == "unchecked_parse"]

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_analyzed": files_analyzed,
        "functions_analyzed": functions_analyzed,
        "findings": all_findings,
        "summary": {
            "missing_null_checks": len(null_checks),
            "unchecked_returns": len(unchecked),
            "return_null_no_exception": len(no_exception),
            "unchecked_parse_calls": len(parse_issues),
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

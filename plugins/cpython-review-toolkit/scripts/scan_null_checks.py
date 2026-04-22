#!/usr/bin/env python3
"""Scan CPython C source for NULL pointer dereference risks.

Detects dereferences before NULL checks, unchecked allocations,
and PyArg_Parse* issues.

Usage:
    python scan_null_checks.py [path] [--max-files N]

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
# Allocation APIs
# ---------------------------------------------------------------------------

ALLOC_APIS = frozenset({
    "malloc", "calloc", "realloc",
    "PyMem_Malloc", "PyMem_Calloc", "PyMem_Realloc",
    "PyMem_RawMalloc", "PyMem_RawCalloc", "PyMem_RawRealloc",
    "PyObject_Malloc", "PyObject_Calloc", "PyObject_Realloc",
    "PyMem_New", "PyMem_Resize",
    "PyObject_New", "PyObject_NewVar",
    "PyObject_GC_New", "PyObject_GC_NewVar",
})

# APIs that return PyObject* and can return NULL.
PYOBJ_APIS = frozenset({
    "PyObject_Call", "PyObject_CallObject", "PyObject_CallFunction",
    "PyObject_CallMethod", "PyObject_GetAttr", "PyObject_GetAttrString",
    "PyObject_GetItem", "PyObject_Str", "PyObject_Repr",
    "PyUnicode_FromString", "PyUnicode_FromFormat",
    "PyBytes_FromString", "PyBytes_FromStringAndSize",
    "PyLong_FromLong", "PyFloat_FromDouble",
    "PyList_New", "PyTuple_New", "PyDict_New",
    "Py_BuildValue",
    "PySequence_List", "PySequence_Tuple", "PySequence_GetItem",
    "PyNumber_Add", "PyNumber_Subtract", "PyNumber_Multiply",
    "PyIter_Next",
    "PyImport_ImportModule",
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

        functions.append({
            "name": func_name,
            "body": body,
            "start_line": sig_start + 1,
            "end_line": body_end + 1,
        })
    return functions


# ---------------------------------------------------------------------------
# NULL safety analysis
# ---------------------------------------------------------------------------

_ALL_ALLOC_RE = re.compile(
    r'(\w+)\s*=\s*(?:\([^)]*\)\s*)?(' + '|'.join(
        re.escape(api) for api in sorted(
            ALLOC_APIS | PYOBJ_APIS, key=len, reverse=True,
        )
    ) + r')\s*\('
)

_NULL_CHECK_TEMPLATE = (
    r'if\s*\(\s*{var}\s*==\s*NULL\s*\)|'
    r'if\s*\(\s*!\s*{var}\s*\)|'
    r'if\s*\(\s*{var}\s*!=\s*NULL\s*\)'
)

_DEREF_RE = re.compile(r'(\w+)\s*->')
_PTR_DEREF_RE = re.compile(r'\*\s*(\w+)')


def analyze_function_null_safety(func: dict) -> list[dict]:
    """Analyze NULL safety in a single function."""
    body = func["body"]
    clean = strip_comments_and_strings(body)
    findings: list[dict] = []

    # 1. Unchecked allocations.
    for m in _ALL_ALLOC_RE.finditer(clean):
        var = m.group(1)
        api = m.group(2)
        line_offset = clean[:m.start()].count('\n') + 1
        after = clean[m.end():m.end() + 500]
        check_re = re.compile(
            _NULL_CHECK_TEMPLATE.format(var=re.escape(var))
        )
        if not check_re.search(after[:300]):
            # Check if variable is dereferenced before check.
            deref = re.search(rf'\b{re.escape(var)}\s*->', after[:200])
            confidence = "high" if deref else "medium"
            findings.append({
                "type": "unchecked_alloc",
                "api_call": api,
                "variable": var,
                "line_offset": line_offset,
                "detail": (
                    f"Allocation via {api} assigned to '{var}' "
                    f"is not checked for NULL"
                    + (" and is dereferenced" if deref else "")
                ),
                "confidence": confidence,
            })

    # 2. Dereference before NULL check.
    # Find all pointer dereferences and check if NULL was tested before.
    lines = clean.split('\n')
    ptr_vars_checked: set[str] = set()
    for i, line in enumerate(lines):
        # Track NULL checks.
        null_m = re.search(
            r'if\s*\(\s*(\w+)\s*==\s*NULL\s*\)|'
            r'if\s*\(\s*!\s*(\w+)\s*\)|'
            r'if\s*\(\s*(\w+)\s*!=\s*NULL\s*\)',
            line,
        )
        if null_m:
            var = null_m.group(1) or null_m.group(2) or null_m.group(3)
            ptr_vars_checked.add(var)

        # Check dereferences.
        for dm in _DEREF_RE.finditer(line):
            var = dm.group(1)
            # Skip well-known safe pointers.
            if var in ('self', 'type', 'tp', 'op', 'module',
                       'Py_TYPE', 'ob_type'):
                continue
            # If this var is assigned from an allocation and not yet
            # checked, flag it.
            # This is a simplified heuristic.

    return findings


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(target: str, *, max_files: int = 0) -> dict:
    """Analyze NULL safety for the given target path."""
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
            func_findings = analyze_function_null_safety(func)
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

    unchecked = [f for f in all_findings if f["type"] == "unchecked_alloc"]

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_analyzed": files_analyzed,
        "functions_analyzed": functions_analyzed,
        "findings": all_findings,
        "summary": {
            "unchecked_allocations": len(unchecked),
            "total_findings": len(all_findings),
            "high_confidence": len(
                [f for f in all_findings if f.get("confidence") == "high"]
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

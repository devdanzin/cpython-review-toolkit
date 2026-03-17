#!/usr/bin/env python3
"""Measure complexity metrics for C functions in CPython source files.

Outputs a JSON structure with per-function metrics:
- line_count, nesting_depth, cyclomatic_complexity
- parameter_count, local_variable_count, goto_count, switch_case_count
- weighted score (1-10)

Usage:
    python measure_c_complexity.py [path]

    path: directory, file, or omitted for current directory
"""

import json
import re
import sys
from pathlib import Path
from typing import Generator


# ---------------------------------------------------------------------------
# Shared utilities (duplicated per-script for zero cross-imports)
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
    """Remove C comments and string/char literals from source."""
    source = re.sub(r'/\*.*?\*/', ' ', source, flags=re.DOTALL)
    source = re.sub(r'//[^\n]*', ' ', source)
    source = re.sub(r'"(?:[^"\\]|\\.)*"', '""', source)
    source = re.sub(r"'(?:[^'\\]|\\.)*'", "''", source)
    return source


# ---------------------------------------------------------------------------
# C function detection
# ---------------------------------------------------------------------------

# Match function definitions in CPython style:
#   return_type\nfunction_name(params)\n{
# or single-line: return_type function_name(params) {
_FUNC_START_RE = re.compile(
    r'^(\w[\w\s\*]+?)\s*\n'          # return type line
    r'^(\w+)\s*\(([^)]*)\)\s*\n'     # name(params) line
    r'^\{',                           # opening brace
    re.MULTILINE,
)

# Simpler fallback: identifier( at column 0, followed by { within 2 lines.
_FUNC_SIMPLE_RE = re.compile(
    r'^(\w+)\s*\(([^)]*)\)\s*(?:\n\s*)?\{',
    re.MULTILINE,
)


def find_functions(source: str) -> list[dict]:
    """Find C function definitions and extract their bodies.

    Returns list of dicts with: name, params, body, start_line, end_line.
    """
    lines = source.split('\n')
    functions: list[dict] = []

    # Strategy: find opening braces at column 0, then work backwards
    # to find the function signature.
    for i, line in enumerate(lines):
        if not line.startswith('{'):
            continue
        # Look backwards for the function signature.
        # Typical pattern: name(params) on line i-1, return type on i-2.
        if i < 1:
            continue
        prev = lines[i - 1].strip()
        # Check if previous line looks like name(params)
        m = re.match(r'^(\w+)\s*\(([^)]*)\)\s*$', prev)
        if not m:
            # Try: return_type name(params) on same line
            m = re.match(r'^(?:\w[\w\s\*]*?)\s+(\w+)\s*\(([^)]*)\)\s*$', prev)
        if not m:
            continue
        func_name = m.group(1)
        params = m.group(2).strip()
        # Skip common false positives.
        if func_name in ('if', 'for', 'while', 'switch', 'do', 'else',
                         'sizeof', 'return', 'typedef', 'struct', 'union',
                         'enum', 'defined'):
            continue

        # Find the matching closing brace.
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
        # Determine start line (return type might be 1-2 lines above).
        sig_start = i - 1
        if sig_start > 0 and re.match(r'^[\w\s\*]+$', lines[sig_start - 1].strip()):
            # Previous line looks like a return type.
            sig_start -= 1

        functions.append({
            "name": func_name,
            "params": params,
            "body": body,
            "start_line": sig_start + 1,  # 1-indexed
            "end_line": body_end + 1,
        })
    return functions


# ---------------------------------------------------------------------------
# Complexity metrics
# ---------------------------------------------------------------------------

_BRANCH_KEYWORDS = re.compile(
    r'\b(if|else\s+if|case|for|while|do)\b'
)
_LOGICAL_OPS = re.compile(r'(&&|\|\|)')
_TERNARY = re.compile(r'\?')
_GOTO = re.compile(r'\bgoto\b')
_SWITCH_CASE = re.compile(r'\bcase\b')
_LOCAL_VAR = re.compile(
    r'^\s+(?:(?:static|const|volatile|unsigned|signed|long|short|register)\s+)*'
    r'(?:int|char|float|double|void|Py_ssize_t|size_t|PyObject|'
    r'Py_hash_t|Py_uhash_t|uint\d+_t|int\d+_t|long|short|unsigned)\s*\*?\s+\w+',
    re.MULTILINE,
)


def measure_function(func: dict) -> dict:
    """Compute complexity metrics for a single C function."""
    body = func["body"]
    clean = strip_comments_and_strings(body)
    body_lines = [l for l in clean.split('\n') if l.strip()]
    line_count = len(body_lines)

    # Parameter count.
    params = func["params"].strip()
    if params and params != "void":
        param_count = params.count(',') + 1
    else:
        param_count = 0

    # Cyclomatic complexity: branches + logical ops + ternary + 1.
    branches = len(_BRANCH_KEYWORDS.findall(clean))
    logical = len(_LOGICAL_OPS.findall(clean))
    ternary = len(_TERNARY.findall(clean))
    cyclomatic = branches + logical + ternary + 1

    # Nesting depth.
    max_depth = 0
    depth = 0
    for ch in clean:
        if ch == '{':
            depth += 1
            max_depth = max(max_depth, depth)
        elif ch == '}':
            depth = max(0, depth - 1)

    # Goto count.
    goto_count = len(_GOTO.findall(clean))

    # Switch-case count.
    switch_case_count = len(_SWITCH_CASE.findall(clean))

    # Local variable count.
    local_var_count = len(_LOCAL_VAR.findall(clean))

    # Weighted score (1-10).
    score = 1.0
    if line_count > 200:
        score += min((line_count - 200) / 100, 3.0)
    elif line_count > 100:
        score += (line_count - 100) / 100 * 1.5
    elif line_count > 50:
        score += (line_count - 50) / 100

    if max_depth > 5:
        score += min((max_depth - 5) * 0.5, 2.0)
    elif max_depth > 3:
        score += (max_depth - 3) * 0.25

    if cyclomatic > 20:
        score += min((cyclomatic - 20) / 10, 2.5)
    elif cyclomatic > 10:
        score += (cyclomatic - 10) / 20

    if param_count > 6:
        score += min((param_count - 6) * 0.3, 1.0)

    if goto_count > 5:
        score += min((goto_count - 5) * 0.2, 0.5)

    score = min(max(round(score, 1), 1.0), 10.0)

    return {
        "name": func["name"],
        "start_line": func["start_line"],
        "end_line": func["end_line"],
        "line_count": line_count,
        "nesting_depth": max_depth,
        "cyclomatic_complexity": cyclomatic,
        "parameter_count": param_count,
        "local_variable_count": local_var_count,
        "goto_count": goto_count,
        "switch_case_count": switch_case_count,
        "score": score,
    }


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(target: str, *, max_files: int = 0) -> dict:
    """Analyze C function complexity for the given target path."""
    target_path = Path(target).resolve()
    project_root = find_cpython_root(target_path)
    if project_root is None:
        project_root = target_path if target_path.is_dir() else target_path.parent
    scan_root = target_path if target_path.is_dir() else target_path.parent

    files_data: list[dict] = []
    total_functions = 0
    hotspot_count = 0

    for filepath in discover_c_files(scan_root, max_files=max_files):
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        functions = find_functions(source)
        if not functions:
            continue

        rel = str(filepath.relative_to(project_root))
        file_entry: dict = {"file": rel, "functions": []}

        for func in functions:
            metrics = measure_function(func)
            total_functions += 1
            if metrics["score"] >= 5.0:
                hotspot_count += 1
            file_entry["functions"].append(metrics)

        if file_entry["functions"]:
            files_data.append(file_entry)

    # Collect all functions and sort by score descending.
    all_funcs = []
    for f in files_data:
        for fn in f["functions"]:
            all_funcs.append({**fn, "file": f["file"]})
    all_funcs.sort(key=lambda x: -x["score"])

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "functions_analyzed": total_functions,
        "files": files_data,
        "hotspots": all_funcs[:30],
        "summary": {
            "total_functions": total_functions,
            "hotspot_count": hotspot_count,
            "avg_cyclomatic": (
                round(
                    sum(fn["cyclomatic_complexity"] for fn in all_funcs)
                    / max(len(all_funcs), 1),
                    1,
                )
            ),
            "avg_line_count": (
                round(
                    sum(fn["line_count"] for fn in all_funcs)
                    / max(len(all_funcs), 1),
                    1,
                )
            ),
            "max_nesting": max(
                (fn["nesting_depth"] for fn in all_funcs), default=0
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

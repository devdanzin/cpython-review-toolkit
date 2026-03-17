#!/usr/bin/env python3
"""Scan CPython C source for GIL discipline issues.

Detects mismatched BEGIN/END_ALLOW_THREADS, Python API calls without
the GIL, blocking calls with the GIL held, and PyGILState balance.

Usage:
    python scan_gil_usage.py [path]

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
# GIL macros and patterns
# ---------------------------------------------------------------------------

_BEGIN_ALLOW = re.compile(r'\bPy_BEGIN_ALLOW_THREADS\b')
_END_ALLOW = re.compile(r'\bPy_END_ALLOW_THREADS\b')
_GILSTATE_ENSURE = re.compile(r'\bPyGILState_Ensure\b')
_GILSTATE_RELEASE = re.compile(r'\bPyGILState_Release\b')

# Python C API calls that require the GIL.
_PYTHON_API_RE = re.compile(
    r'\b(Py_(?:INCREF|DECREF|XINCREF|XDECREF|CLEAR|SETREF|BuildValue|'
    r'Initialize|Finalize)|'
    r'Py[A-Z]\w+|'
    r'_Py[A-Z]\w+)\s*\('
)

# Blocking I/O calls.
BLOCKING_CALLS = frozenset({
    "read", "write", "recv", "send", "recvfrom", "sendto",
    "sleep", "usleep", "nanosleep",
    "select", "poll", "epoll_wait", "kevent",
    "flock", "lockf", "fcntl",
    "connect", "accept", "listen", "bind",
    "waitpid", "wait", "waitid",
    "fgets", "fread", "fwrite", "fflush",
    "getaddrinfo", "gethostbyname",
    "popen", "system",
    "pthread_mutex_lock", "pthread_cond_wait",
    "sem_wait",
})

_BLOCKING_RE = re.compile(
    r'\b(' + '|'.join(re.escape(c) for c in sorted(BLOCKING_CALLS)) + r')\s*\('
)


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
# GIL analysis
# ---------------------------------------------------------------------------

def analyze_function_gil(func: dict) -> list[dict]:
    """Analyze GIL discipline in a single function."""
    body = func["body"]
    clean = strip_comments_and_strings(body)
    findings: list[dict] = []

    # 1. Check BEGIN/END_ALLOW_THREADS balance.
    begins = list(_BEGIN_ALLOW.finditer(clean))
    ends = list(_END_ALLOW.finditer(clean))
    if len(begins) != len(ends):
        findings.append({
            "type": "mismatched_allow_threads",
            "line_offset": (
                clean[:begins[0].start()].count('\n') + 1 if begins
                else 0
            ),
            "detail": (
                f"Mismatched Py_BEGIN_ALLOW_THREADS ({len(begins)}) / "
                f"Py_END_ALLOW_THREADS ({len(ends)})"
            ),
            "confidence": "high",
        })

    # 2. Check for Python API calls within GIL-released regions.
    for bi, begin in enumerate(begins):
        # Find matching end.
        end_pos = len(clean)
        if bi < len(ends):
            end_pos = ends[bi].start()
        region = clean[begin.end():end_pos]
        region_offset = clean[:begin.start()].count('\n')

        for api_m in _PYTHON_API_RE.finditer(region):
            api_name = api_m.group(1)
            # Filter false positives: some internal helpers are GIL-free.
            if api_name.startswith('Py_UNREACHABLE'):
                continue
            if api_name in ('Py_BEGIN_ALLOW_THREADS', 'Py_END_ALLOW_THREADS'):
                continue
            line_offset = region_offset + region[:api_m.start()].count('\n') + 1
            findings.append({
                "type": "api_without_gil",
                "api_call": api_name,
                "line_offset": line_offset,
                "detail": (
                    f"Python C API call {api_name}() within "
                    f"Py_BEGIN_ALLOW_THREADS region (GIL not held)"
                ),
                "confidence": "high",
            })

    # 3. Check for blocking calls without GIL release.
    # Only flag if function has no BEGIN_ALLOW_THREADS at all,
    # or the blocking call is outside any released region.
    for bm in _BLOCKING_RE.finditer(clean):
        call_name = bm.group(1)
        call_pos = bm.start()
        # Check if this call is within a GIL-released region.
        in_released = False
        for bi, begin in enumerate(begins):
            end_pos = ends[bi].start() if bi < len(ends) else len(clean)
            if begin.end() <= call_pos <= end_pos:
                in_released = True
                break
        if not in_released:
            line_offset = clean[:call_pos].count('\n') + 1
            findings.append({
                "type": "blocking_with_gil",
                "api_call": call_name,
                "line_offset": line_offset,
                "detail": (
                    f"Blocking call {call_name}() without releasing GIL "
                    f"(no Py_BEGIN_ALLOW_THREADS)"
                ),
                "confidence": "medium",
            })

    # 4. Check PyGILState_Ensure / Release balance.
    ensures = list(_GILSTATE_ENSURE.finditer(clean))
    releases = list(_GILSTATE_RELEASE.finditer(clean))
    if len(ensures) != len(releases):
        findings.append({
            "type": "mismatched_gilstate",
            "line_offset": (
                clean[:ensures[0].start()].count('\n') + 1 if ensures
                else 0
            ),
            "detail": (
                f"Mismatched PyGILState_Ensure ({len(ensures)}) / "
                f"PyGILState_Release ({len(releases)})"
            ),
            "confidence": "high",
        })

    return findings


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(target: str, *, max_files: int = 0) -> dict:
    """Analyze GIL discipline for the given target path."""
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
            func_findings = analyze_function_gil(func)
            for finding in func_findings:
                finding["file"] = rel
                finding["function"] = func["name"]
                finding["line"] = func["start_line"] + finding.pop("line_offset")
                all_findings.append(finding)

    mismatched = [f for f in all_findings if "mismatched" in f["type"]]
    api_without = [f for f in all_findings if f["type"] == "api_without_gil"]
    blocking = [f for f in all_findings if f["type"] == "blocking_with_gil"]

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_analyzed": files_analyzed,
        "functions_analyzed": functions_analyzed,
        "findings": all_findings,
        "summary": {
            "mismatched_pairs": len(mismatched),
            "api_without_gil": len(api_without),
            "blocking_with_gil": len(blocking),
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

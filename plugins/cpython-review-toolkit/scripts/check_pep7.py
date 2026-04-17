#!/usr/bin/env python3
"""Check adherence to PEP 7 (C style guide for CPython).

Outputs a JSON structure with per-file style violations:
- indentation (4 spaces, no tabs)
- line length (79 chars max)
- brace style, keyword spacing, operator spacing
- trailing whitespace, missing braces

Usage:
    python check_pep7.py [path] [--max-files N]

    path: directory, file, or omitted for current directory
    --max-files N: cap the number of .c/.h files scanned (0 = unlimited)
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


# ---------------------------------------------------------------------------
# Comment / string tracking for line-by-line analysis
# ---------------------------------------------------------------------------

def _build_line_mask(source: str) -> list[bool]:
    """Return a per-line mask: True if the line is inside a multiline comment."""
    lines = source.split('\n')
    mask = [False] * len(lines)
    in_comment = False
    for i, line in enumerate(lines):
        if in_comment:
            mask[i] = True
            if '*/' in line:
                in_comment = False
        else:
            # Check for /* without matching */ on same line.
            stripped = line
            # Remove string literals first.
            stripped = re.sub(r'"(?:[^"\\]|\\.)*"', '""', stripped)
            stripped = re.sub(r"'(?:[^'\\]|\\.)*'", "''", stripped)
            # Remove single-line comments.
            stripped = re.sub(r'//.*$', '', stripped)
            if '/*' in stripped:
                idx = stripped.index('/*')
                rest = stripped[idx + 2:]
                if '*/' not in rest:
                    in_comment = True
                    mask[i] = True
    return mask


def _strip_line_strings_and_comments(line: str) -> str:
    """Remove string literals and comments from a single line."""
    line = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
    line = re.sub(r"'(?:[^'\\]|\\.)*'", "''", line)
    line = re.sub(r'//.*$', '', line)
    # Partial: remove /* ... */ on single line.
    line = re.sub(r'/\*.*?\*/', ' ', line)
    return line


# ---------------------------------------------------------------------------
# PEP 7 checks
# ---------------------------------------------------------------------------

_CONTROL_KEYWORDS = ('if', 'for', 'while', 'switch', 'do')

# Keyword followed by ( without space: if(, for(, while(, switch(
_KEYWORD_NO_SPACE = re.compile(
    r'\b(' + '|'.join(_CONTROL_KEYWORDS) + r')\('
)

# Keywords that legitimately have a space before paren.
_SPACE_BEFORE_PAREN_OK = frozenset({
    *_CONTROL_KEYWORDS, 'return', 'sizeof', 'typeof', 'defined',
    '__attribute__', '__declspec', 'else',
})

# Tabs in indentation.
_TAB_INDENT = re.compile(r'^\t')

# Trailing whitespace.
_TRAILING_WS = re.compile(r'[ \t]+$')

# Missing braces: if/for/while followed by a single statement
# (heuristic: next non-blank line is not { and not blank).
_CONTROL_NO_BRACE = re.compile(
    r'^\s+(?:if|for|while)\s*\(.*\)\s*$'
)


def check_file(source: str) -> list[dict]:
    """Check a single file for PEP 7 violations.

    Returns list of violation dicts: line, rule, message.
    """
    lines = source.split('\n')
    comment_mask = _build_line_mask(source)
    violations: list[dict] = []

    for i, raw_line in enumerate(lines):
        lineno = i + 1

        # Skip lines inside multiline comments.
        if comment_mask[i]:
            continue

        clean = _strip_line_strings_and_comments(raw_line)

        # 1. Trailing whitespace.
        if _TRAILING_WS.search(raw_line) and raw_line.strip():
            violations.append({
                "line": lineno,
                "rule": "trailing-whitespace",
                "message": "Trailing whitespace",
            })

        # 2. Tab indentation.
        if _TAB_INDENT.match(raw_line):
            violations.append({
                "line": lineno,
                "rule": "tab-indent",
                "message": "Tab indentation (PEP 7 requires 4 spaces)",
            })

        # 3. Line length > 79.
        if len(raw_line) > 79:
            violations.append({
                "line": lineno,
                "rule": "line-too-long",
                "message": f"Line length {len(raw_line)} > 79",
            })

        # 4. Missing space after control keyword.
        if _KEYWORD_NO_SPACE.search(clean):
            violations.append({
                "line": lineno,
                "rule": "keyword-space",
                "message": "Missing space between keyword and parenthesis",
            })

        # 5. Space before function call paren (heuristic).
        # Only flag identifier + space + ( patterns, excluding keywords.
        m = re.search(r'\b(\w+)\s+\(', clean)
        if m and m.group(1) not in (
            *_CONTROL_KEYWORDS, 'return', 'sizeof', 'typeof', 'defined',
            '__attribute__', '__declspec', 'do', 'else',
        ):
            # Exclude function declarations (return type + name pattern).
            if not re.match(r'^\s*(?:static|extern|inline|const|volatile'
                            r'|unsigned|signed|long|short|void|int|char'
                            r'|float|double|PyObject|Py_ssize_t|size_t)',
                            clean):
                violations.append({
                    "line": lineno,
                    "rule": "func-call-space",
                    "message": (
                        f"Space before '(' in function call '{m.group(1)} ('"
                    ),
                })

        # 6. Missing braces after control statement.
        if _CONTROL_NO_BRACE.match(raw_line):
            # Check if next non-blank line starts with {.
            for j in range(i + 1, min(i + 3, len(lines))):
                next_stripped = lines[j].strip()
                if not next_stripped:
                    continue
                if next_stripped.startswith('{') or next_stripped.endswith('{'):
                    break
                # Next line is a statement without braces.
                violations.append({
                    "line": lineno,
                    "rule": "missing-braces",
                    "message": "Control statement without braces",
                })
                break

    return violations


# ---------------------------------------------------------------------------
# Header guard check
# ---------------------------------------------------------------------------

def check_header_guard(source: str, filepath: str) -> list[dict]:
    """Check that .h files have proper include guards."""
    if not filepath.endswith('.h'):
        return []
    violations = []
    has_ifndef = bool(re.search(r'^\s*#\s*ifndef\s+\w+', source, re.MULTILINE))
    has_define = bool(re.search(r'^\s*#\s*define\s+\w+', source, re.MULTILINE))
    has_pragma_once = bool(
        re.search(r'^\s*#\s*pragma\s+once', source, re.MULTILINE)
    )
    if not (has_ifndef and has_define) and not has_pragma_once:
        violations.append({
            "line": 1,
            "rule": "header-guard",
            "message": "Header file missing include guard (#ifndef/#define or #pragma once)",
        })
    return violations


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(target: str, *, max_files: int = 0) -> dict:
    """Analyze PEP 7 style compliance for the given target path."""
    target_path = Path(target).resolve()
    project_root = find_cpython_root(target_path)
    if project_root is None:
        project_root = target_path if target_path.is_dir() else target_path.parent
    scan_root = target_path if target_path.is_dir() else target_path.parent

    files_data: list[dict] = []
    total_violations = 0
    rule_counts: dict[str, int] = {}
    files_analyzed = 0

    for filepath in discover_c_files(scan_root, max_files=max_files):
        files_analyzed += 1
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        rel = str(filepath.relative_to(project_root))
        violations = check_file(source)
        violations.extend(check_header_guard(source, rel))

        if violations:
            files_data.append({
                "file": rel,
                "violation_count": len(violations),
                "violations": violations,
            })
            total_violations += len(violations)
            for v in violations:
                rule_counts[v["rule"]] = rule_counts.get(v["rule"], 0) + 1

    # Sort files by violation count descending.
    files_data.sort(key=lambda x: -x["violation_count"])

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_analyzed": files_analyzed,
        "files": files_data,
        "summary": {
            "total_violations": total_violations,
            "files_with_violations": len(files_data),
            "rule_counts": dict(
                sorted(rule_counts.items(), key=lambda x: -x[1])
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

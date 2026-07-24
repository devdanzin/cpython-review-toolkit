#!/usr/bin/env python3
"""Check adherence to PEP 7 (C style guide for CPython).

Emits the toolkit's standard report envelope (``findings: [...]``), with the
legacy per-file ``files: [...]`` grouping kept alongside for back-compat.

**Calibrated against CPython's own code.** Run unmodified over ``Objects/``
this checker used to emit 5,736 violations of which essentially none were
actionable. The rules are now split into two tiers:

*Whole-tree rules* -- always on, because PEP 7 states them unconditionally and
CPython's own code honours them:

``tab-indent``           "Use 4-space indents and no tabs at all."
``trailing-whitespace``  "No line should end in whitespace."
``keyword-space``        "one space between keywords like ``if``, ``for`` and
                         the following left paren".
``header-guard``         Not a PEP 7 rule at all (PEP 7 says nothing about
                         include guards) but a real CPython convention; scoped
                         to hand-written headers only.

*Diff-gated rules* -- reported only for lines a change actually touches
(``--diff-only`` / ``--diff-ref`` / ``--changed-files``), or when forced on
with ``--enable-rule``:

``missing-braces``   PEP 7 requires braces "but do not add them to code you
                     are not otherwise modifying. All new C code requires
                     braces." A whole-tree run of this rule reports something
                     PEP 7 explicitly tells you not to change (2,199 hits on
                     ``Objects/``).
``line-too-long``    PEP 7 says 79 columns, but it is a soft rule ("rules are
                     there to be broken... to be consistent with surrounding
                     code that also breaks it") and CPython's ``.editorconfig``
                     deliberately sets no ``max_line_length``. CPython ships
                     1,285 over-length lines in ``Objects/*.c`` alone. Requires
                     an explicit ``--line-limit N`` on top of the diff gate.

The ``func-call-space`` rule was **removed**. PEP 7 does forbid a space before
a call's open paren ("Function and macro call style: ``foo(a, b, c)`` -- no
space before the open paren"), so the *category* was legitimate, but the purely
lexical ``IDENT (`` implementation could not tell a call from a macro
definition and was actively harmful: it fired on
``#define MAX_INTMAX_CHARS (5 + ...)``, where closing the space converts an
object-like macro into a function-like one and changes its meaning, and on
``#elif (SIZEOF_SIZE_T == 4)`` and ``assert (...)``.

This module deliberately imports **only the standard library** -- no
tree-sitter, no shared comment-stripping helper. That independence is why it
is the one scanner in the toolkit with verified-exact line numbers (280/280 on
a hand-checked sample); the report envelope below is therefore assembled
locally rather than via ``scan_common.build_report``, which would pull in the
tree-sitter chassis.

Usage:
    python check_pep7.py [path] [--max-files N]
                         [--diff-only] [--diff-ref REF]
                         [--changed-files a.c,b.c]
                         [--line-limit N] [--enable-rule NAME[,NAME]]
"""

import json
import re
import subprocess
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
# Rule tiers
# ---------------------------------------------------------------------------

# Reported on any run.
WHOLE_TREE_RULES = frozenset({
    "tab-indent", "trailing-whitespace", "keyword-space", "header-guard",
})

# Reported only within a diff scope (or when forced via --enable-rule).
DIFF_GATED_RULES = frozenset({"missing-braces", "line-too-long"})

ALL_RULES = WHOLE_TREE_RULES | DIFF_GATED_RULES

# PEP 7 severities. Only the rules PEP 7 states unconditionally are FIX; the
# ones it qualifies ("do not add them to code you are not otherwise
# modifying") or that CPython declines to automate are CONSIDER.
RULE_SEVERITY = {
    "tab-indent": "FIX",
    "trailing-whitespace": "FIX",
    "keyword-space": "FIX",
    "header-guard": "CONSIDER",
    "missing-braces": "CONSIDER",
    "line-too-long": "CONSIDER",
}


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
            stripped = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', stripped)
            stripped = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", stripped)
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
    line = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', line)
    line = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", line)
    line = re.sub(r'//.*$', '', line)
    # Partial: remove /* ... */ on single line.
    line = re.sub(r'/\*.*?\*/', ' ', line)
    return line


# ---------------------------------------------------------------------------
# PEP 7 checks
# ---------------------------------------------------------------------------

# PEP 7: "one space between keywords like if, for and the following left
# paren". `do` never takes a paren, so it is not in this set; the `while` of a
# `do { ... } while (0)` idiom is caught by `while`.
_CONTROL_KEYWORDS = ('if', 'for', 'while', 'switch')

# Keyword followed by ( without space: if(, for(, while(, switch(
_KEYWORD_NO_SPACE = re.compile(
    r'\b(' + '|'.join(_CONTROL_KEYWORDS) + r')\('
)

# Preprocessor conditionals are not control statements: `#if(defined X)` is
# not the `if (` of PEP 7's rule.
_PREPROCESSOR_LINE = re.compile(r'^\s*#')

# Tabs in indentation.
_TAB_INDENT = re.compile(r'^\t')

# Trailing whitespace.
_TRAILING_WS = re.compile(r'[ \t]+$')

# Missing braces: if/for/while followed by a single statement
# (heuristic: next non-blank line is not { and not blank).
_CONTROL_NO_BRACE = re.compile(
    r'^\s+(?:if|for|while)\s*\(.*\)\s*$'
)

DEFAULT_LINE_LIMIT = 79


def check_file(
    source: str,
    *,
    rules: frozenset[str] | None = None,
    line_limit: int = 0,
    changed_lines: set[int] | None = None,
) -> list[dict]:
    """Check a single file for PEP 7 violations.

    ``rules`` restricts which rule names may be emitted (default: the
    whole-tree tier). ``line_limit`` enables ``line-too-long`` when > 0.
    ``changed_lines`` (1-indexed) restricts the diff-gated rules to lines a
    change actually touched; ``None`` means "no diff scope".

    Returns list of violation dicts: line, rule, message.
    """
    if rules is None:
        rules = WHOLE_TREE_RULES
    lines = source.split('\n')
    comment_mask = _build_line_mask(source)
    violations: list[dict] = []

    def in_diff_scope(lineno: int) -> bool:
        return changed_lines is None or lineno in changed_lines

    for i, raw_line in enumerate(lines):
        lineno = i + 1

        # Skip lines inside multiline comments.
        if comment_mask[i]:
            continue

        clean = _strip_line_strings_and_comments(raw_line)

        # 1. Trailing whitespace.
        if "trailing-whitespace" in rules:
            if _TRAILING_WS.search(raw_line) and raw_line.strip():
                violations.append({
                    "line": lineno,
                    "rule": "trailing-whitespace",
                    "message": "Trailing whitespace",
                })

        # 2. Tab indentation.
        if "tab-indent" in rules and _TAB_INDENT.match(raw_line):
            violations.append({
                "line": lineno,
                "rule": "tab-indent",
                "message": "Tab indentation (PEP 7 requires 4 spaces)",
            })

        # 3. Line length (diff-gated, and off unless --line-limit is given).
        if (
            "line-too-long" in rules
            and line_limit > 0
            and len(raw_line) > line_limit
            and in_diff_scope(lineno)
        ):
            violations.append({
                "line": lineno,
                "rule": "line-too-long",
                "message": f"Line length {len(raw_line)} > {line_limit}",
            })

        # 4. Missing space after control keyword.
        if (
            "keyword-space" in rules
            and not _PREPROCESSOR_LINE.match(raw_line)
            and _KEYWORD_NO_SPACE.search(clean)
        ):
            violations.append({
                "line": lineno,
                "rule": "keyword-space",
                "message": "Missing space between keyword and parenthesis",
            })

        # 5. Missing braces after control statement (diff-gated: PEP 7 says
        #    "do not add them to code you are not otherwise modifying").
        if (
            "missing-braces" in rules
            and in_diff_scope(lineno)
            and _CONTROL_NO_BRACE.match(raw_line)
        ):
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

# Markers that identify a machine-generated header.
_GENERATED_MARKERS = (
    "[clinic input]",
    "clinic end generated code",
    "file generated by",
    "auto-generated by",
    "automatically generated",
    "this file is generated",
    "generated by tools/",
    "do not edit",
)

# Path components whose headers are textual includes, not standalone headers.
# A guard on these would make the second inclusion a no-op and break the build:
# `stringlib/*.h` is included once per character width, and Argument Clinic
# output (`*.c.h`) is `#include`d mid-file exactly once.
_MULTI_INCLUDE_DIRS = frozenset({"stringlib", "clinic"})

# Vendored third-party trees. These follow their upstream's style, not PEP 7.
_VENDORED_DIRS = frozenset({
    "_decimal", "libmpdec", "_hacl", "expat", "zlib", "zlib-ng",
    "_blake2", "_sha3", "cjkcodecs", "_ctypes",
})


def is_vendored(filepath: str) -> bool:
    """True if ``filepath`` lives in a vendored third-party tree."""
    return bool(set(Path(filepath).parts) & _VENDORED_DIRS)


def is_generated_or_textual_header(source: str, filepath: str) -> bool:
    """True if ``filepath`` is generated or is a textual (re-)include.

    These must not be flagged for a missing include guard: for a
    ``stringlib/`` template header a guard would actively break the build,
    and generated files are not hand-edited.
    """
    path = Path(filepath)
    if path.name.endswith(".c.h"):
        return True
    if set(path.parts) & _MULTI_INCLUDE_DIRS:
        return True
    if is_vendored(filepath):
        return True
    head = source[:4000].lower()
    return any(marker in head for marker in _GENERATED_MARKERS)


def check_header_guard(source: str, filepath: str) -> list[dict]:
    """Check that hand-written .h files have proper include guards.

    Note this is a CPython *convention*, not a PEP 7 rule -- PEP 7 says
    nothing about include guards -- hence CONSIDER severity.
    """
    if not filepath.endswith('.h'):
        return []
    if is_generated_or_textual_header(source, filepath):
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
            "message": (
                "Header file missing include guard "
                "(#ifndef/#define or #pragma once)"
            ),
        })
    return violations


# ---------------------------------------------------------------------------
# Diff scope
# ---------------------------------------------------------------------------

_DIFF_FILE_RE = re.compile(r'^\+\+\+ b/(.+)$')
_DIFF_HUNK_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')


def parse_diff(diff_text: str) -> dict[str, set[int]]:
    """Map ``repo-relative path -> set of touched (post-image) line numbers``.

    Parses ``git diff --unified=0`` output. Only the ``+`` side matters: a
    diff-gated rule should fire on the lines a change introduces, never on the
    untouched code around them.
    """
    scope: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff_text.split('\n'):
        m = _DIFF_FILE_RE.match(line)
        if m:
            current = m.group(1)
            scope.setdefault(current, set())
            continue
        m = _DIFF_HUNK_RE.match(line)
        if m and current is not None:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            scope[current].update(range(start, start + count))
    return {k: v for k, v in scope.items() if v}


def diff_scope_from_git(project_root: Path, ref: str) -> dict[str, set[int]]:
    """Return the changed-line scope of ``ref`` in ``project_root``.

    Raises ``RuntimeError`` if git is unavailable or the ref is unknown, so
    the caller can report the failure rather than silently scanning nothing.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "diff", "--unified=0", ref,
             "--", "*.c", "*.h"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"git diff failed: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"git diff {ref} failed: {proc.stderr.strip() or 'unknown error'}"
        )
    return parse_diff(proc.stdout)


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(
    target: str,
    *,
    max_files: int = 0,
    diff_only: bool = False,
    diff_ref: str = "HEAD",
    changed_files: list[str] | None = None,
    line_limit: int = 0,
    enable_rules: frozenset[str] | None = None,
) -> dict:
    """Analyze PEP 7 style compliance for the given target path.

    With ``diff_only`` (or ``changed_files``) the diff-gated rules switch on
    and are restricted to the touched lines. ``enable_rules`` force-enables
    named diff-gated rules on a whole-tree run.
    """
    target_path = Path(target).resolve()
    project_root = find_cpython_root(target_path)
    if project_root is None:
        project_root = target_path if target_path.is_dir() else target_path.parent
    # scan_root is the target itself: discover_c_files() yields just that file
    # for a file root. Using .parent here silently widened a single-file scan
    # to the entire directory.
    scan_root = target_path

    enable_rules = enable_rules or frozenset()
    diff_scope: dict[str, set[int]] | None = None
    diff_error: str | None = None

    if changed_files:
        # Whole-file scope for each named file.
        diff_scope = {f: set() for f in changed_files}
        diff_only = True
    elif diff_only:
        try:
            diff_scope = diff_scope_from_git(project_root, diff_ref)
        except RuntimeError as exc:
            diff_error = str(exc)
            diff_scope = {}

    active_rules = set(WHOLE_TREE_RULES)
    if diff_only:
        active_rules |= DIFF_GATED_RULES
    active_rules |= (enable_rules & ALL_RULES)
    if line_limit <= 0:
        active_rules.discard("line-too-long")
    rules = frozenset(active_rules)

    files_data: list[dict] = []
    findings: list[dict] = []
    total_violations = 0
    rule_counts: dict[str, int] = {}
    files_analyzed = 0

    for filepath in discover_c_files(scan_root, max_files=max_files):
        try:
            rel = str(filepath.relative_to(project_root))
        except ValueError:
            rel = str(filepath)

        if diff_scope is not None and rel not in diff_scope:
            continue

        files_analyzed += 1
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        changed_lines: set[int] | None = None
        if diff_scope is not None:
            # An empty set means "whole file" (explicit --changed-files).
            changed_lines = diff_scope[rel] or None

        violations = check_file(
            source,
            rules=rules,
            line_limit=line_limit,
            changed_lines=changed_lines,
        )
        if "header-guard" in rules:
            violations.extend(check_header_guard(source, rel))
        violations.sort(key=lambda v: (v["line"], v["rule"]))

        if violations:
            files_data.append({
                "file": rel,
                "violation_count": len(violations),
                "violations": violations,
            })
            total_violations += len(violations)
            for v in violations:
                rule_counts[v["rule"]] = rule_counts.get(v["rule"], 0) + 1
                findings.append({
                    "type": v["rule"],
                    "rule": v["rule"],
                    "file": rel,
                    "line": v["line"],
                    "severity": RULE_SEVERITY.get(v["rule"], "CONSIDER"),
                    "confidence": "high",
                    "detail": v["message"],
                })

    # Sort files by violation count descending.
    files_data.sort(key=lambda x: -x["violation_count"])
    findings.sort(key=lambda f: (f["file"], f["line"]))

    summary: dict = {
        "total_findings": len(findings),
        "total_violations": total_violations,
        "files_with_violations": len(files_data),
        "rule_counts": dict(
            sorted(rule_counts.items(), key=lambda x: -x[1])
        ),
        "by_severity": _count_by(findings, "severity"),
        "active_rules": sorted(rules),
        "skipped_rules": sorted(ALL_RULES - rules),
        "diff_scope": (
            "whole-tree" if diff_scope is None
            else f"{len(diff_scope)} changed file(s)"
        ),
    }
    if diff_error:
        summary["diff_error"] = diff_error

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_analyzed": files_analyzed,
        # This checker is line-based, not function-based; the key is present
        # for envelope parity with the tree-sitter scanners.
        "functions_analyzed": 0,
        "findings": findings,
        "summary": summary,
        # Back-compat: the original per-file grouping.
        "files": files_data,
    }


def _count_by(findings: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f[key]] = counts.get(f[key], 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def main() -> None:
    max_files = 0
    diff_only = False
    diff_ref = "HEAD"
    changed_files: list[str] = []
    line_limit = 0
    enable_rules: set[str] = set()
    positional: list[str] = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--max-files" and i + 1 < len(argv):
            max_files = int(argv[i + 1])
            i += 2
        elif argv[i] == "--diff-only":
            diff_only = True
            i += 1
        elif argv[i] == "--diff-ref" and i + 1 < len(argv):
            diff_ref = argv[i + 1]
            diff_only = True
            i += 2
        elif argv[i] == "--changed-files" and i + 1 < len(argv):
            changed_files = [p for p in argv[i + 1].split(",") if p]
            i += 2
        elif argv[i] == "--line-limit" and i + 1 < len(argv):
            line_limit = int(argv[i + 1])
            i += 2
        elif argv[i] == "--enable-rule" and i + 1 < len(argv):
            enable_rules.update(r for r in argv[i + 1].split(",") if r)
            i += 2
        elif argv[i].startswith("--"):
            i += 1
        else:
            positional.append(argv[i])
            i += 1
    target = positional[0] if positional else "."
    result = analyze(
        target,
        max_files=max_files,
        diff_only=diff_only,
        diff_ref=diff_ref,
        changed_files=changed_files,
        line_limit=line_limit,
        enable_rules=frozenset(enable_rules),
    )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

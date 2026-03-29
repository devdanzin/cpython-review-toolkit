#!/usr/bin/env python3
"""Analyze git history for churn metrics, commit classification, and co-change data.

CPython-specific version: uses regex for C function boundary detection
(consistent with other cpython-review-toolkit scripts), extended commit
classification keywords for C bug types, and CPython module family awareness.

Outputs a JSON structure with:
- file_churn: files ranked by commit count with churn rates
- function_churn: functions ranked by commit count
- recent_fixes/features/refactors: commit details with diffs
- co_change_clusters: files that tend to change together

Usage:
    python analyze_history.py [path] [options]

Options:
    --days N          Analyze last N days (default: 90)
    --since DATE      Start date (ISO format, overrides --days)
    --until DATE      End date (ISO format, default: today)
    --last N          Analyze exactly the last N commits
    --max-commits N   Cap total commits analyzed (default: 2000)
    --no-function     Skip function-level churn (file-level only, faster)
"""

import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator


# ---------------------------------------------------------------------------
# Commit classification — extended for CPython C bug patterns
# ---------------------------------------------------------------------------

CLASSIFICATION_RULES: list[tuple[str, list[str]]] = [
    ("fix", [
        "fix", "bug", "patch", "resolve", "issue", "crash",
        "error", "broken", "repair", "correct", "regression",
        "workaround", "hotfix",
        # CPython C-specific
        "segfault", "leak", "null", "refcount", "decref", "incref",
        "use-after-free", "double-free", "overflow", "underflow",
        "gil", "deadlock",
    ]),
    ("docs", [
        "doc", "readme", "comment", "typo", "spelling",
        "changelog", "documentation", "what's new",
    ]),
    ("test", [
        "test", "coverage", "assert", "mock", "fixture",
    ]),
    ("refactor", [
        "refactor", "clean", "simplify", "reorganize",
        "restructure", "rename", "move", "extract",
        "deduplicate", "inline", "modernize",
        # CPython-specific
        "clinic", "argument clinic", "stable abi",
    ]),
    ("chore", [
        "bump", "dependency", "update", "upgrade", "ci",
        "config", "lint", "format", "version", "release",
        "merge", "revert",
    ]),
    ("feature", [
        "add", "implement", "new", "feature", "introduce",
        "support", "enable", "create",
    ]),
]

# ---------------------------------------------------------------------------
# CPython module families — related modules that should share bug fixes
# ---------------------------------------------------------------------------

CPYTHON_MODULE_FAMILIES: dict[str, list[str]] = {
    "hash": [
        "Modules/md5module.c",
        "Modules/sha1module.c",
        "Modules/sha2module.c",
        "Modules/sha3module.c",
        "Modules/blake2module.c",
        "Modules/hmacmodule.c",
        "Modules/_hashopenssl.c",
    ],
    "dbm": [
        "Modules/_dbmmodule.c",
        "Modules/_gdbmmodule.c",
    ],
    "io": [
        "Modules/_io/bufferedio.c",
        "Modules/_io/bytesio.c",
        "Modules/_io/fileio.c",
        "Modules/_io/stringio.c",
        "Modules/_io/textio.c",
        "Modules/_io/winconsoleio.c",
    ],
    "codec": [
        "Modules/cjkcodecs/multibytecodec.c",
        "Modules/unicodedata.c",
    ],
    "async": [
        "Modules/_asynciomodule.c",
        "Modules/selectmodule.c",
        "Modules/overlapped.c",
    ],
    "sqlite": [
        "Modules/_sqlite/connection.c",
        "Modules/_sqlite/cursor.c",
        "Modules/_sqlite/statement.c",
        "Modules/_sqlite/prepare_protocol.c",
        "Modules/_sqlite/microprotocols.c",
        "Modules/_sqlite/module.c",
    ],
    "ctypes": [
        "Modules/_ctypes/_ctypes.c",
        "Modules/_ctypes/callbacks.c",
        "Modules/_ctypes/callproc.c",
    ],
    "zstd": [
        "Modules/_zstd/compressor.c",
        "Modules/_zstd/decompressor.c",
        "Modules/_zstd/zstddict.c",
    ],
    "core_objects": [
        "Objects/listobject.c",
        "Objects/dictobject.c",
        "Objects/setobject.c",
        "Objects/tupleobject.c",
    ],
    "core_numbers": [
        "Objects/longobject.c",
        "Objects/floatobject.c",
        "Objects/complexobject.c",
    ],
    "core_strings": [
        "Objects/unicodeobject.c",
        "Objects/bytesobject.c",
        "Objects/bytearrayobject.c",
    ],
}

_GIT_TIMEOUT = 30
_SCRIPT_START: float = 0.0
_SCRIPT_TIMEOUT = 300
_MAX_DIFF_LINES_FIX = 150
_MAX_DIFF_LINES_REFACTOR = 80


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def find_cpython_root(start: Path) -> Path | None:
    """Walk up from *start* looking for CPython root markers."""
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


def find_project_root(start: Path) -> Path:
    """Find project root — CPython root or git root or start dir."""
    cpython_root = find_cpython_root(start)
    if cpython_root:
        return cpython_root
    # Fall back to git root.
    current = start if start.is_dir() else start.parent
    for _ in range(20):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return start if start.is_dir() else start.parent


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def classify_commit(message: str) -> str:
    """Classify a commit message into a category."""
    msg_lower = message.lower()
    for category, keywords in CLASSIFICATION_RULES:
        for keyword in keywords:
            if keyword in msg_lower:
                return category
    return "unknown"


def _run_git(args: list[str], cwd: Path, timeout: int = _GIT_TIMEOUT):
    return subprocess.run(
        ["git"] + args, capture_output=True, text=True,
        cwd=str(cwd), timeout=timeout,
    )


def _run_git_streaming(args: list[str], cwd: Path):
    return subprocess.Popen(
        ["git"] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(cwd),
    )


def _is_git_repo(path: Path) -> bool:
    try:
        result = _run_git(
            ["rev-parse", "--is-inside-work-tree"], path, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _check_script_timeout() -> bool:
    return (time.monotonic() - _SCRIPT_START) > _SCRIPT_TIMEOUT


def _get_file_line_count(filepath: Path) -> int:
    try:
        return len(
            filepath.read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# C function boundary detection (regex-based, consistent with other scripts)
# ---------------------------------------------------------------------------

_SKIP_NAMES = frozenset({
    "if", "for", "while", "switch", "do", "else",
    "sizeof", "return", "typedef", "struct", "union",
    "enum", "defined",
})


def get_c_function_boundaries(filepath: Path) -> list[dict]:
    """Get C function boundaries using regex.

    Handles multi-line signatures and Argument Clinic comments.
    """
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    lines = source.split("\n")
    functions: list[dict] = []

    for i, line in enumerate(lines):
        if not line.startswith("{"):
            continue
        if i < 1:
            continue

        # Look backwards up to 10 lines to assemble the signature.
        sig_lines: list[str] = []
        sig_start = i - 1
        for k in range(i - 1, max(i - 11, -1), -1):
            stripped = lines[k].strip()
            if not stripped or stripped.startswith("/*") or stripped.startswith("*"):
                continue
            sig_lines.insert(0, stripped)
            if "(" in stripped:
                sig_start = k
                break

        if not sig_lines:
            continue

        sig = " ".join(sig_lines)
        sig = re.sub(r"/\*\[clinic.*?\]\*/", "", sig).strip()

        m = re.match(r"(?:[\w\s\*]+?)\s+(\w+)\s*\(([^)]*)\)\s*$", sig)
        if not m:
            m = re.match(r"^(\w+)\s*\(([^)]*)\)\s*$", sig)
        if not m:
            continue

        func_name = m.group(1)
        if func_name in _SKIP_NAMES:
            continue

        # Find matching closing brace.
        depth = 1
        body_end = i + 1
        for j in range(i + 1, len(lines)):
            for ch in lines[j]:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        body_end = j
                        break
            if depth == 0:
                break

        # Handle #ifdef brace imbalance: if we never found closing brace,
        # estimate end as next function start or 500 lines, whichever is first.
        if depth != 0:
            body_end = min(i + 500, len(lines) - 1)

        functions.append({
            "name": func_name,
            "line_start": sig_start + 1,
            "line_end": body_end + 1,
        })

    return functions


def get_py_function_boundaries(filepath: Path) -> list[dict]:
    """Get Python function boundaries using AST."""
    import ast
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, OSError):
        return []

    functions = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_lineno = getattr(node, "end_lineno", node.lineno)
            functions.append({
                "name": node.name,
                "line_start": node.lineno,
                "line_end": end_lineno,
            })
    return functions


def get_function_boundaries(filepath: Path) -> list[dict]:
    """Get function boundaries — C via regex, Python via AST."""
    if filepath.suffix in (".c", ".h"):
        return get_c_function_boundaries(filepath)
    elif filepath.suffix == ".py":
        return get_py_function_boundaries(filepath)
    return []


# ---------------------------------------------------------------------------
# Git log parsing
# ---------------------------------------------------------------------------

_EXCLUDE_DIRS = frozenset({
    ".git", ".tox", ".venv", "venv", "__pycache__",
    "node_modules", "build", "dist", ".eggs",
})


def parse_git_log(
    lines, max_commits: int, project_root: Path | None = None,
) -> tuple[list[dict], list[dict]]:
    """Parse git log --numstat output into commits and file churn stats."""
    commits: list[dict] = []
    file_changes: dict[str, dict] = {}
    current_commit: dict | None = None
    commit_count = 0

    for line in lines:
        line = line.rstrip("\n")
        if line.startswith("COMMIT:"):
            if current_commit is not None:
                commits.append(current_commit)
            commit_count += 1
            if commit_count > max_commits:
                break
            parts = line[7:].split("|", 3)
            if len(parts) < 4:
                current_commit = None
                continue
            commit_hash, date_str, author, message = parts
            current_commit = {
                "hash": commit_hash, "date": date_str,
                "author": author, "message": message,
                "type": classify_commit(message),
                "files": [], "stats": [],
            }
        elif line.strip() and current_commit is not None:
            parts = line.split("\t", 2)
            if len(parts) == 3:
                added_str, removed_str, filepath = parts
                try:
                    added = int(added_str) if added_str != "-" else 0
                    removed = int(removed_str) if removed_str != "-" else 0
                except ValueError:
                    continue
                current_commit["files"].append(filepath)
                current_commit["stats"].append({
                    "file": filepath, "added": added, "removed": removed,
                })
                if filepath not in file_changes:
                    file_changes[filepath] = {
                        "commits": 0, "lines_added": 0,
                        "lines_removed": 0, "authors": set(),
                        "first_date": date_str, "last_date": date_str,
                    }
                fc = file_changes[filepath]
                fc["commits"] += 1
                fc["lines_added"] += added
                fc["lines_removed"] += removed
                fc["authors"].add(author)
                if date_str < fc["first_date"]:
                    fc["first_date"] = date_str
                if date_str > fc["last_date"]:
                    fc["last_date"] = date_str

    if current_commit is not None and commit_count <= max_commits:
        commits.append(current_commit)

    file_stats: list[dict] = []
    for filepath, fc in file_changes.items():
        line_count = (
            _get_file_line_count(project_root / filepath)
            if project_root else 0
        )
        churn_rate = (
            round((fc["lines_added"] + fc["lines_removed"]) / line_count, 2)
            if line_count > 0 else 0.0
        )
        file_stats.append({
            "file": filepath,
            "commits": fc["commits"],
            "lines_added": fc["lines_added"],
            "lines_removed": fc["lines_removed"],
            "churn_rate": churn_rate,
            "authors": len(fc["authors"]),
            "first_commit_in_range": fc["first_date"],
            "last_modified": fc["last_date"],
        })

    file_stats.sort(key=lambda x: x["commits"], reverse=True)
    return commits, file_stats


# ---------------------------------------------------------------------------
# Function-level churn
# ---------------------------------------------------------------------------

def compute_function_churn(
    commits: list[dict],
    scan_root: Path,
    project_root: Path,
    *,
    max_files: int = 0,
) -> list[dict]:
    """Map diff hunks to C/Python function boundaries."""
    file_functions: dict[str, list[dict]] = {}

    if scan_root.is_file():
        all_files = [scan_root]
    else:
        all_files = sorted(
            p for p in scan_root.rglob("*")
            if p.is_file() and p.suffix in (".c", ".h", ".py")
        )

    filtered = []
    for f in all_files:
        try:
            parts = set(f.relative_to(project_root).parts)
        except ValueError:
            continue
        if parts & _EXCLUDE_DIRS:
            continue
        filtered.append(f)

    if max_files > 0:
        filtered = filtered[:max_files]

    for f in filtered:
        rel_path = str(f.relative_to(project_root))
        boundaries = get_function_boundaries(f)
        if boundaries:
            file_functions[rel_path] = boundaries

    if not file_functions:
        return []

    func_commits: dict[tuple[str, str], set[str]] = defaultdict(set)
    for commit in commits:
        if _check_script_timeout():
            break
        for file_path in commit["files"]:
            if file_path not in file_functions:
                continue
            try:
                diff_result = _run_git(
                    ["show", "--format=", "-U0", commit["hash"],
                     "--", file_path],
                    project_root,
                )
                if diff_result.returncode != 0:
                    continue
            except subprocess.TimeoutExpired:
                continue

            changed_lines: set[int] = set()
            for diff_line in diff_result.stdout.splitlines():
                hunk = re.match(
                    r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", diff_line,
                )
                if hunk:
                    start = int(hunk.group(1))
                    count = int(hunk.group(2)) if hunk.group(2) else 1
                    changed_lines.update(range(start, start + count))

            for func in file_functions[file_path]:
                func_range = set(
                    range(func["line_start"], func["line_end"] + 1)
                )
                if changed_lines & func_range:
                    func_commits[(file_path, func["name"])].add(
                        commit["hash"]
                    )

    results = []
    for (file_path, func_name), commit_hashes in func_commits.items():
        boundaries = file_functions.get(file_path, [])
        func_info = next(
            (f for f in boundaries if f["name"] == func_name), None,
        )
        results.append({
            "function": func_name,
            "file": file_path,
            "line_start": func_info["line_start"] if func_info else 0,
            "line_end": func_info["line_end"] if func_info else 0,
            "commits": len(commit_hashes),
        })

    results.sort(key=lambda x: x["commits"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Commit details and co-change clusters
# ---------------------------------------------------------------------------

def _relative_scope(scan_root: Path, project_root: Path) -> str:
    try:
        rel = scan_root.resolve().relative_to(project_root.resolve())
        return str(rel) if str(rel) != "." else "."
    except ValueError:
        return "."


def _truncate_diff(diff_text: str, max_lines: int) -> str:
    lines = diff_text.splitlines()
    if len(lines) <= max_lines:
        return diff_text
    return "\n".join(lines[:max_lines]) + "\n[diff truncated]"


def get_commit_details(
    commits: list[dict],
    commit_type: str,
    project_root: Path,
    scan_root: Path,
    max_diff_lines: int,
) -> list[dict]:
    """Get details (including diffs) for commits of a given type."""
    typed = [c for c in commits if c["type"] == commit_type]
    results = []
    rel_scope = _relative_scope(scan_root, project_root)

    for commit in typed:
        if _check_script_timeout():
            break
        diff_args = ["show", "--format=", "--patch", commit["hash"], "--"]
        if rel_scope != ".":
            diff_args.append(rel_scope)
        try:
            dr = _run_git(diff_args, project_root)
            diff_text = dr.stdout if dr.returncode == 0 else ""
        except subprocess.TimeoutExpired:
            diff_text = "[diff unavailable: timeout]"

        diff_text = _truncate_diff(diff_text, max_diff_lines)

        results.append({
            "commit": commit["hash"],
            "commit_short": commit["hash"][:7],
            "message": commit["message"],
            "date": commit["date"],
            "author": commit["author"],
            "files": commit["files"],
            "diff": diff_text,
        })
    return results


def compute_co_change_clusters(
    commits: list[dict],
    min_co_changes: int = 3,
    max_pairs: int = 30,
) -> list[dict]:
    """Detect file pairs that frequently change together."""
    file_commit_counts: dict[str, int] = defaultdict(int)
    co_changes: dict[tuple[str, str], int] = defaultdict(int)

    for commit in commits:
        files = sorted(set(commit["files"]))
        for f in files:
            file_commit_counts[f] += 1
        for i in range(len(files)):
            for j in range(i + 1, len(files)):
                co_changes[(files[i], files[j])] += 1

    results = []
    for (a, b), count in co_changes.items():
        if count >= min_co_changes:
            results.append({
                "file_a": a,
                "file_b": b,
                "co_change_count": count,
                "total_commits_a": file_commit_counts[a],
                "total_commits_b": file_commit_counts[b],
            })
    results.sort(key=lambda x: x["co_change_count"], reverse=True)
    return results[:max_pairs]


# ---------------------------------------------------------------------------
# Module family detection
# ---------------------------------------------------------------------------

def get_module_family(filepath: str) -> str | None:
    """Return the module family name for a file, or None."""
    for family, members in CPYTHON_MODULE_FAMILIES.items():
        if filepath in members:
            return family
    return None


def get_family_members(filepath: str) -> list[str]:
    """Return sibling files in the same module family."""
    for members in CPYTHON_MODULE_FAMILIES.values():
        if filepath in members:
            return [m for m in members if m != filepath]
    return []


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> dict:
    args: dict = {
        "path": ".", "days": 90, "since": None, "until": None,
        "last": None, "max_commits": 2000, "max_files": 0,
        "no_function": False,
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--days" and i + 1 < len(argv):
            args["days"] = int(argv[i + 1])
            i += 2
        elif arg == "--since" and i + 1 < len(argv):
            args["since"] = argv[i + 1]
            i += 2
        elif arg == "--until" and i + 1 < len(argv):
            args["until"] = argv[i + 1]
            i += 2
        elif arg == "--last" and i + 1 < len(argv):
            args["last"] = int(argv[i + 1])
            i += 2
        elif arg == "--max-commits" and i + 1 < len(argv):
            args["max_commits"] = int(argv[i + 1])
            i += 2
        elif arg == "--max-files" and i + 1 < len(argv):
            args["max_files"] = int(argv[i + 1])
            i += 2
        elif arg == "--no-function":
            args["no_function"] = True
            i += 1
        elif not arg.startswith("-"):
            args["path"] = arg
            i += 1
        else:
            i += 1
    return args


def analyze(argv: list[str] | None = None) -> dict:
    """Analyze git history for churn metrics and commit classification."""
    global _SCRIPT_START
    _SCRIPT_START = time.monotonic()

    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)

    scan_root = Path(args["path"]).resolve()
    project_root = find_project_root(scan_root)

    if not _is_git_repo(project_root):
        return {
            "error": "Not a git repository",
            "project_root": str(project_root),
        }

    now = datetime.now(timezone.utc)
    since = args["since"] or (
        now - timedelta(days=args["days"])
    ).isoformat()
    until = args["until"] or now.isoformat()

    last_n = args["last"]
    max_commits = args["max_commits"]

    git_args = ["log", "--numstat", "--format=COMMIT:%H|%aI|%an|%s"]
    if last_n is not None:
        git_args.append(f"-{last_n}")
    else:
        git_args.extend([f"--since={since}", f"--until={until}"])
    git_args.append("--")
    rel_scope = _relative_scope(scan_root, project_root)
    if rel_scope != ".":
        git_args.append(rel_scope)

    proc = _run_git_streaming(git_args, project_root)
    try:
        commits, file_churn = parse_git_log(
            proc.stdout, max_commits, project_root,
        )
    finally:
        proc.wait()

    commit_cap_applied = len(commits) >= max_commits
    if last_n is not None and commits:
        since = commits[-1]["date"]
        until = commits[0]["date"]
        try:
            days = max(
                1,
                (datetime.fromisoformat(until)
                 - datetime.fromisoformat(since)).days,
            )
        except ValueError:
            days = args["days"]
    else:
        days = args["days"]

    commits_by_type: dict[str, int] = defaultdict(int)
    authors: set[str] = set()
    for c in commits:
        commits_by_type[c["type"]] += 1
        authors.add(c["author"])

    function_churn: list[dict] = []
    function_churn_note: str | None = None
    if args["no_function"] or _check_script_timeout():
        function_churn_note = "Function-level churn skipped"
    else:
        function_churn = compute_function_churn(
            commits, scan_root, project_root,
            max_files=args["max_files"],
        )

    recent_fixes = get_commit_details(
        commits, "fix", project_root, scan_root, _MAX_DIFF_LINES_FIX,
    )
    recent_features = get_commit_details(
        commits, "feature", project_root, scan_root, _MAX_DIFF_LINES_FIX,
    )
    recent_refactors = get_commit_details(
        commits, "refactor", project_root, scan_root,
        _MAX_DIFF_LINES_REFACTOR,
    )

    co_change_clusters = compute_co_change_clusters(commits)

    # Annotate file churn with module family info.
    for fc in file_churn:
        family = get_module_family(fc["file"])
        if family:
            fc["module_family"] = family

    result: dict = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "time_range": {
            "start": since,
            "end": until,
            "days": days,
            "commit_cap_applied": commit_cap_applied,
        },
        "summary": {
            "total_commits": len(commits),
            "commits_by_type": dict(commits_by_type),
            "files_changed": len(file_churn),
            "functions_changed": len(function_churn),
            "authors": len(authors),
        },
        "file_churn": file_churn,
        "function_churn": function_churn,
        "recent_fixes": recent_fixes,
        "recent_features": recent_features,
        "recent_refactors": recent_refactors,
        "co_change_clusters": co_change_clusters,
        "module_families": CPYTHON_MODULE_FAMILIES,
    }

    if function_churn_note:
        result["function_churn_note"] = function_churn_note

    return result


def main() -> None:
    try:
        result = analyze()
        if "error" in result:
            json.dump(result, sys.stdout, indent=2)
            sys.stdout.write("\n")
            sys.exit(1)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:
        json.dump(
            {"error": str(e), "type": type(e).__name__},
            sys.stdout, indent=2,
        )
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()

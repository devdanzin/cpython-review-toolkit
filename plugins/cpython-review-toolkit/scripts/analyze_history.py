#!/usr/bin/env python3
"""Analyze git history for churn metrics, commit classification, and co-change data.

CPython-specific version: uses regex for C function boundary detection
(consistent with other cpython-review-toolkit scripts), extended commit
classification keywords for C bug types, and CPython module family awareness.

Outputs a JSON structure with:
- file_churn: files ranked by commit count with churn rates and bug-fix density
- function_churn: functions ranked by commit count
- recent_fixes/features/refactors: commit details with diffs
- co_change_clusters: files that tend to change together
- watchlist: files ranked by recent crash-fix density per KLOC (prefer this
  over churn_rate — raw churn is a poor proxy for defect density)
- notes: warnings that would otherwise be silent (shallow clone, commit cap,
  script timeout, skipped function churn)

Usage:
    python analyze_history.py [path] [options]

Options:
    --days N          Analyze last N days (default: 90; CPython-scale windows
                      want several thousand — the full history is ~13000 days)
    --since DATE      Start date (ISO format, overrides --days)
    --until DATE      End date (ISO format, default: today)
    --last N          Analyze exactly the last N commits
    --max-commits N   Cap total commits analyzed (default: 50000)
    --max-files N     Cap files scanned for function boundaries (default: all)
    --workers N       Parallel git subprocess workers (default: 8)
    --no-function     Skip function-level churn (file-level only, faster)
    --no-density      Skip the per-file bug-fix-density pass (faster)
    --no-follow       Do not pass --follow when counting per-file fix history
                      (faster; understates churn for renamed files)
    --density-top N   Files to compute bug-fix density for (default: 40)
    --density-days N  Recency window for the density metric (default: 730)
    --introduced-by FILE:LINE
                      Report the commit that introduced a line (and the line's
                      full change history), then exit. Wraps `git blame -L` and
                      `git log -L`. Accepts FILE:LINE or FILE:START-END.

Unrecognized options are a hard error — a silently-ignored window flag (e.g.
`--months 420` when the flag is `--days`) yields a confident, wrong analysis.
"""

import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator


# ---------------------------------------------------------------------------
# Commit classification — extended for CPython C bug patterns
# ---------------------------------------------------------------------------

CLASSIFICATION_RULES: list[tuple[str, list[str]]] = [
    # Deliberately narrow. The over-broad terms this list used to carry
    # ("error", "issue", "patch", "correct", "null", "gil") pushed 45% of
    # CPython's Objects/ history into the `fix` bucket, which is noise, not
    # signal. Weak-but-real fix commits are recovered by score_fix() below.
    ("fix", [
        "fix", "bug", "resolve", "crash",
        "broken", "repair", "regression",
        "workaround", "hotfix",
        # CPython C-specific
        "segfault", "leak", "refcount", "decref", "incref",
        "use-after-free", "double-free", "overflow", "underflow",
        "deadlock",
    ]),
    ("docs", [
        "doc", "readme", "comment", "typo", "spelling",
        "changelog", "documentation", "what's new", "whatsnew",
        "docstring",
    ]),
    ("test", [
        "test", "coverage", "assert", "mock", "fixture",
    ]),
    ("refactor", [
        "refactor", "clean", "simplify", "reorganize",
        "restructure", "rename", "move", "extract",
        "deduplicate", "inline", "modernize", "signature",
        "py_newref", "py_setref",
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
# Crash-class taxonomy and fix scoring
#
# The old classifier matched substrings anywhere in the message and took the
# first hit, so a hygiene sweep ("Use Py_NewRef() in Objects/") and a real
# use-after-free fix landed in the same bucket. These tables re-score a message
# by *what kind of defect it names*, then demote pure-hygiene wording.
# ---------------------------------------------------------------------------

CRASH_CLASS_PATTERNS: list[tuple[str, str]] = [
    ("use-after-free", r"use[-\s]?after[-\s]?free|\buaf\b|dangling|freed memory"),
    ("double-free", r"double[-\s]?free"),
    ("crash", (
        r"\bcrash(?:es|ed|ing)?\b|segfault|segmentation fault|\bsigsegv\b"
        r"|\bsigabrt\b|fatal (?:error|python error)|core dump|hard crash"
    )),
    ("memory-leak", r"\brefleak|reference leak|memory leak|\bleak(?:s|ed|ing)?\b"),
    ("refcount", (
        r"\brefcount|reference count|\bincref\b|\bdecref\b"
        r"|py_(?:x?decref|incref|clear)\b"
    )),
    ("null-deref", (
        r"null (?:pointer )?deref|deref(?:erence)?[^.]{0,20}\bnull\b"
        r"|\bnullptr\b|null check|check for null|returning null"
    )),
    ("overflow", (
        r"\boverflow|\bunderflow|out[-\s]of[-\s]bounds|\boob\b"
        r"|buffer over(?:run|flow)"
    )),
    ("data-race", (
        r"\bdata race\b|\brace condition\b|\bracy\b|thread[-\s]?saf"
        r"|\btsan\b|\bdeadlock\b|\bhang(?:s|ing)?\b"
    )),
    ("uninitialized", r"uninitiali[sz]ed|\bgarbage (?:value|member|pointer)"),
    ("recursion", r"stack overflow|recursion (?:limit|guard|depth)|infinite recursion"),
    ("assertion", r"assertion (?:failure|error)|\bassert(?:ion)? fail"),
    ("corruption", r"\bcorrupt(?:ion|ed|s)?\b"),
]

_CRASH_CLASS_RES: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(pattern)) for name, pattern in CRASH_CLASS_PATTERNS
]

# Wording that marks a mechanical/hygiene sweep. These commits routinely carry
# a "fix" verb without fixing a defect, and they are the bulk of the false
# positives in the `fix` bucket.
_HYGIENE_RE = re.compile(
    r"py_x?newref|py_x?setref|\bsignature(?:s)?\b|docstring|\btypo\b"
    r"|what'?s ?new|whatsnew|pep[- ]?585|argument clinic|\bclinic\b"
    r"|remove (?:unused|redundant|dead|obsolete|useless)"
    r"|\bmodernize\b|\bcleanup\b|clean up|\brename[sd]?\b|\brefactor"
    r"|\bwhitespace\b|\bformatting\b|\blint\b|\bspelling\b"
    r"|post-release|bump version|update copyright|\bdocumentation\b"
    r"|merge branch|merge remote"
)

_FIX_VERB_RE = re.compile(
    r"\bfix(?:e[sd])?\b|\bbug(?:s)?\b|\bregression\b|\bhotfix\b"
    r"|\bworkaround\b|\bbroken\b|\brepair(?:s|ed)?\b|\bresolve[sd]?\b"
)

_ISSUE_REF_RE = re.compile(r"\b(?:gh|bpo)-\d+\b|\bissue #?\d+\b")

_FIX_CONFIDENCE_RANK: dict[str, int] = {
    "high": 0, "medium": 1, "low": 2, "none": 3,
}


def crash_class(message: str) -> str | None:
    """Return the crash/safety class a commit message names, or None.

    A non-None value means the message describes a memory-safety, concurrency,
    or crash-shaped defect — the population a crash-hunting review cares about.
    """
    msg = message.lower()
    for name, pattern in _CRASH_CLASS_RES:
        if pattern.search(msg):
            return name
    return None


def score_fix(message: str) -> tuple[str, str | None]:
    """Score how confidently a commit message describes a real defect fix.

    Returns ``(fix_confidence, crash_class)`` where confidence is one of
    ``high`` / ``medium`` / ``low`` / ``none``. Naming a crash class is worth
    more than a bare "fix" verb; hygiene wording demotes hard.
    """
    msg = message.lower()
    klass = crash_class(msg)
    score = 0
    if klass:
        score += 2
        if _ISSUE_REF_RE.search(msg):
            score += 1
        if _FIX_VERB_RE.search(msg):
            score += 1
    elif _FIX_VERB_RE.search(msg):
        score += 1
    if _HYGIENE_RE.search(msg):
        score -= 3

    if score >= 3:
        confidence = "high"
    elif score == 2:
        confidence = "medium"
    elif score == 1:
        confidence = "low"
    else:
        confidence = "none"
    return confidence, klass

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
_TIMEOUT_HIT = False
_MAX_DIFF_LINES_FIX = 150
_MAX_DIFF_LINES_REFACTOR = 80

# Bug-fix-density defaults. Raw churn was measured *anti-correlated* with
# defect density on CPython's Objects/ (genericaliasobject.c ranked 30th by
# churn, 1st by crash-fixes-per-KLOC), so the density metric is the one the
# watchlist ranks on.
_DENSITY_TOP = 40
_DENSITY_WINDOW_DAYS = 730


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
    """Classify a commit message into a category.

    A message scored ``high``/``medium`` by :func:`score_fix` is a fix
    regardless of wording — that recovers commits like "Handle
    allocate_weakref returning NULL", which name a defect without a fix verb.
    A message demoted to ``none`` by hygiene wording is *never* a fix, which is
    what keeps mechanical sweeps ("Fix function signatures in structseq.c") out
    of the bucket.
    """
    confidence, _ = score_fix(message)
    if confidence in ("high", "medium"):
        return "fix"
    demoted = confidence == "none" and _HYGIENE_RE.search(message.lower())

    msg_lower = message.lower()
    for category, keywords in CLASSIFICATION_RULES:
        if category == "fix" and demoted:
            continue
        for keyword in keywords:
            if keyword in msg_lower:
                return category
    return "unknown"


def _run_git(args: list[str], cwd: Path, timeout: int = _GIT_TIMEOUT):
    # errors="replace": commit messages are not guaranteed UTF-8. CPython's own
    # history contains pre-2015 commits with latin-1 author/subject bytes, which
    # abort a full-history run with UnicodeDecodeError under the default strict
    # decoding. A mangled character in one old subject line is preferable to
    # losing the entire analysis.
    return subprocess.run(
        ["git"] + args, capture_output=True, text=True, errors="replace",
        cwd=str(cwd), timeout=timeout,
    )


def _run_git_streaming(args: list[str], cwd: Path):
    # See _run_git: non-UTF-8 commit messages must not kill the stream.
    return subprocess.Popen(
        ["git"] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", cwd=str(cwd),
    )


def _is_git_repo(path: Path) -> bool:
    try:
        result = _run_git(
            ["rev-parse", "--is-inside-work-tree"], path, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def is_shallow_clone(project_root: Path) -> bool:
    """Detect a shallow clone, which silently truncates every temporal signal.

    Checks ``git rev-parse --is-shallow-repository`` and falls back to the
    presence of ``.git/shallow`` (older git, or a worktree whose git binary
    predates the flag).
    """
    try:
        result = _run_git(
            ["rev-parse", "--is-shallow-repository"], project_root, timeout=5,
        )
        if result.returncode == 0:
            answer = result.stdout.strip().lower()
            if answer in ("true", "false"):
                return answer == "true"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    git_dir = project_root / ".git"
    if git_dir.is_file():  # worktree / submodule pointer file
        return False
    return (git_dir / "shallow").exists()


def _repo_depth(project_root: Path) -> tuple[int, str]:
    """Return ``(total_commits_on_HEAD, first_commit_date)``; zeros on failure."""
    total = 0
    first_date = ""
    try:
        counted = _run_git(["rev-list", "--count", "HEAD"], project_root, timeout=20)
        if counted.returncode == 0:
            total = int(counted.stdout.strip() or 0)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    try:
        # `git log --reverse -1` is a trap: -n is applied *before* --reverse,
        # so it returns the newest commit. Ask for the root commit instead.
        roots = _run_git(
            ["rev-list", "--max-parents=0", "HEAD"], project_root, timeout=20,
        )
        if roots.returncode == 0 and roots.stdout.strip():
            oldest_root = roots.stdout.strip().splitlines()[-1]
            shown = _run_git(
                ["show", "--no-patch", "--format=%aI", oldest_root],
                project_root, timeout=20,
            )
            if shown.returncode == 0:
                first_date = shown.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, IndexError):
        pass
    return total, first_date


def _check_script_timeout() -> bool:
    global _TIMEOUT_HIT
    if (time.monotonic() - _SCRIPT_START) > _SCRIPT_TIMEOUT:
        _TIMEOUT_HIT = True
        return True
    return False


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


#: Suffixes this toolkit analyzes. Deliberately C-only: the target is CPython's
#: C source. The inherited ``.py`` path ran ``ast.parse`` over ``Lib/``,
#: including deliberately-malformed test data, leaking ``SyntaxWarning`` from
#: files like ``Lib/test/test_import/data/syntax_warnings.py`` into the
#: toolkit's own stderr while producing churn data nothing consumes.
SOURCE_SUFFIXES: tuple[str, ...] = (".c", ".h")


def get_function_boundaries(filepath: Path) -> list[dict]:
    """Get function boundaries for a C source or header file."""
    if filepath.suffix in SOURCE_SUFFIXES:
        return get_c_function_boundaries(filepath)
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
            confidence, klass = score_fix(message)
            current_commit = {
                "hash": commit_hash, "date": date_str,
                "author": author, "message": message,
                "type": classify_commit(message),
                "fix_confidence": confidence,
                "crash_class": klass,
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
        entry = {
            "file": filepath,
            "commits": fc["commits"],
            "lines_added": fc["lines_added"],
            "lines_removed": fc["lines_removed"],
            "churn_rate": churn_rate,
            "authors": len(fc["authors"]),
            "first_commit_in_range": fc["first_date"],
            "last_modified": fc["last_date"],
        }
        # A path that no longer exists on disk is a ghost entry (renamed or
        # deleted). It otherwise sits in the top-20 with churn_rate 0.0.
        if project_root is not None and not (project_root / filepath).exists():
            entry["deleted"] = True
        file_stats.append(entry)

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
    workers: int = 8,
) -> list[dict]:
    """Map diff hunks to C/Python function boundaries."""
    file_functions: dict[str, list[dict]] = {}

    if scan_root.is_file():
        all_files = [scan_root]
    else:
        all_files = sorted(
            p for p in scan_root.rglob("*")
            if p.is_file() and p.suffix in SOURCE_SUFFIXES
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

    # Collect all (commit_hash, file_path) pairs that need diffs.
    work_items: list[tuple[str, str]] = []
    for commit in commits:
        for file_path in commit["files"]:
            if file_path in file_functions:
                work_items.append((commit["hash"], file_path))

    def _fetch_hunk(item: tuple[str, str]) -> tuple[str, str, set[int]]:
        """Fetch changed line numbers for one (commit, file) pair."""
        commit_hash, file_path = item
        try:
            diff_result = _run_git(
                ["show", "--format=", "-U0", commit_hash,
                 "--", file_path],
                project_root,
            )
            if diff_result.returncode != 0:
                return commit_hash, file_path, set()
        except subprocess.TimeoutExpired:
            return commit_hash, file_path, set()

        changed_lines: set[int] = set()
        for diff_line in diff_result.stdout.splitlines():
            hunk = re.match(
                r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", diff_line,
            )
            if hunk:
                start = int(hunk.group(1))
                count = int(hunk.group(2)) if hunk.group(2) else 1
                changed_lines.update(range(start, start + count))
        return commit_hash, file_path, changed_lines

    # Run all hunk fetches in parallel.
    func_commits: dict[tuple[str, str], set[str]] = defaultdict(set)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for commit_hash, file_path, changed_lines in pool.map(
            _fetch_hunk, work_items,
        ):
            if _check_script_timeout():
                break
            if not changed_lines:
                continue
            for func in file_functions[file_path]:
                func_range = set(
                    range(func["line_start"], func["line_end"] + 1)
                )
                if changed_lines & func_range:
                    func_commits[(file_path, func["name"])].add(
                        commit_hash
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


def _fetch_one_diff(
    commit_hash: str,
    project_root: Path,
    rel_scope: str,
    max_diff_lines: int,
) -> tuple[str, str]:
    """Fetch the diff for a single commit. Thread-safe."""
    diff_args = ["show", "--format=", "--patch", commit_hash, "--"]
    if rel_scope != ".":
        diff_args.append(rel_scope)
    try:
        dr = _run_git(diff_args, project_root)
        diff_text = dr.stdout if dr.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        diff_text = "[diff unavailable: timeout]"
    return commit_hash, _truncate_diff(diff_text, max_diff_lines)


def get_commit_details(
    commits: list[dict],
    commit_type: str,
    project_root: Path,
    scan_root: Path,
    max_diff_lines: int,
    workers: int = 8,
) -> list[dict]:
    """Get details (including diffs) for commits of a given type.

    Fetches diffs in parallel using a thread pool for speed.
    """
    typed = [c for c in commits if c["type"] == commit_type]
    if not typed:
        return []

    rel_scope = _relative_scope(scan_root, project_root)

    # Fetch all diffs in parallel.
    diff_map: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _fetch_one_diff, c["hash"], project_root,
                rel_scope, max_diff_lines,
            ): c["hash"]
            for c in typed
        }
        for future in as_completed(futures):
            if _check_script_timeout():
                pool.shutdown(wait=False, cancel_futures=True)
                break
            commit_hash, diff_text = future.result()
            diff_map[commit_hash] = diff_text

    # Assemble results in original commit order.
    results = []
    for commit in typed:
        diff_text = diff_map.get(commit["hash"], "")
        results.append({
            "commit": commit["hash"],
            "commit_short": commit["hash"][:7],
            "message": commit["message"],
            "date": commit["date"],
            "author": commit["author"],
            "fix_confidence": commit.get("fix_confidence", "none"),
            "crash_class": commit.get("crash_class"),
            "files": commit["files"],
            "diff": diff_text,
        })
    # Highest-confidence, crash-shaped, most recent fixes first — that is the
    # read order. Two stable passes keep recency as the tie-breaker.
    results.sort(key=lambda r: r["date"], reverse=True)
    results.sort(
        key=lambda r: (
            _FIX_CONFIDENCE_RANK.get(r["fix_confidence"], 3),
            0 if r["crash_class"] else 1,
        )
    )
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
# Per-file bug-fix density (rename-following)
# ---------------------------------------------------------------------------

def _file_fix_history(
    rel_path: str,
    project_root: Path,
    cutoff_iso: str,
    *,
    follow: bool = True,
) -> dict:
    """Count fix / crash-fix commits for one file over its whole history.

    ``--follow`` makes the count survive renames, which understate churn by up
    to 15% on CPython (``tupleobject.c``: 287 without, 329 with).
    """
    args = ["log", "--format=COMMIT:%H|%aI|%s"]
    if follow:
        args.append("--follow")
    args.extend(["--", rel_path])
    try:
        result = _run_git(args, project_root, timeout=_GIT_TIMEOUT)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}
    if result.returncode != 0:
        return {}

    total = fix_commits = crash_fix_commits = crash_fix_recent = 0
    top: list[dict] = []
    for line in result.stdout.splitlines():
        if not line.startswith("COMMIT:"):
            continue
        parts = line[7:].split("|", 2)
        if len(parts) < 3:
            continue
        commit_hash, date_str, subject = parts
        total += 1
        confidence, klass = score_fix(subject)
        if confidence in ("high", "medium"):
            fix_commits += 1
        if klass and confidence in ("high", "medium"):
            crash_fix_commits += 1
            if date_str >= cutoff_iso:
                crash_fix_recent += 1
                if len(top) < 3:
                    top.append({
                        "hash": commit_hash[:12],
                        "date": date_str[:10],
                        "subject": subject,
                        "crash_class": klass,
                    })
    return {
        "history_commits": total,
        "fix_commits": fix_commits,
        "crash_fix_commits": crash_fix_commits,
        "crash_fix_commits_recent": crash_fix_recent,
        "top_crash_fixes": top,
        "follow_renames": follow,
    }


def compute_bugfix_density(
    file_stats: list[dict],
    project_root: Path,
    *,
    top_n: int = _DENSITY_TOP,
    window_days: int = _DENSITY_WINDOW_DAYS,
    workers: int = 8,
    follow: bool = True,
) -> list[dict]:
    """Annotate ``file_stats`` in place with bug-fix density; return a watchlist.

    Density is *recent crash-fix commits per KLOC*, which on the CPython sample
    ranked the actually-defect-bearing files far better than ``churn_rate``.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=window_days)
    ).isoformat()
    candidates = [
        fs for fs in file_stats
        if not fs.get("deleted") and Path(fs["file"]).suffix in SOURCE_SUFFIXES
    ][:top_n]
    if not candidates:
        return []

    def _one(fs: dict) -> tuple[dict, dict]:
        return fs, _file_fix_history(
            fs["file"], project_root, cutoff, follow=follow,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fs, stats in pool.map(_one, candidates):
            if _check_script_timeout():
                break
            if not stats:
                continue
            lines = _get_file_line_count(project_root / fs["file"])
            kloc = lines / 1000.0
            fs.update(stats)
            fs["lines"] = lines
            fs["crash_fix_density"] = (
                round(stats["crash_fix_commits_recent"] / kloc, 2)
                if kloc > 0 else 0.0
            )

    watchlist = [
        {
            "file": fs["file"],
            "crash_fix_density": fs["crash_fix_density"],
            "crash_fix_commits_recent": fs["crash_fix_commits_recent"],
            "crash_fix_commits": fs["crash_fix_commits"],
            "fix_commits": fs["fix_commits"],
            "lines": fs["lines"],
            "commits": fs["commits"],
            "churn_rate": fs["churn_rate"],
            "top_crash_fixes": fs["top_crash_fixes"],
        }
        for fs in candidates
        if "crash_fix_density" in fs
    ]
    watchlist.sort(
        key=lambda w: (w["crash_fix_density"], w["crash_fix_commits_recent"]),
        reverse=True,
    )
    return watchlist


# ---------------------------------------------------------------------------
# --introduced-by: which commit put this line here?
# ---------------------------------------------------------------------------

def parse_line_spec(spec: str) -> tuple[str, int, int]:
    """Parse ``FILE:LINE`` or ``FILE:START-END`` into ``(path, start, end)``."""
    if ":" not in spec:
        raise ValueError(
            f"--introduced-by expects FILE:LINE or FILE:START-END, got {spec!r}"
        )
    path, _, lines = spec.rpartition(":")
    if not path:
        raise ValueError(f"--introduced-by: missing file path in {spec!r}")
    if "-" in lines:
        start_s, _, end_s = lines.partition("-")
    else:
        start_s = end_s = lines
    try:
        start, end = int(start_s), int(end_s)
    except ValueError:
        raise ValueError(
            f"--introduced-by: line numbers must be integers, got {lines!r}"
        ) from None
    if start < 1 or end < start:
        raise ValueError(f"--introduced-by: invalid line range {lines!r}")
    return path, start, end


def _commit_meta(commit_hash: str, project_root: Path) -> dict:
    """Look up one commit's metadata plus the files it touched."""
    result = _run_git(
        ["show", "--no-patch", "--format=%H|%aI|%an|%s", commit_hash],
        project_root,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {"hash": commit_hash}
    parts = result.stdout.strip().splitlines()[0].split("|", 3)
    if len(parts) < 4:
        return {"hash": commit_hash}
    full_hash, date_str, author, subject = parts
    confidence, klass = score_fix(subject)
    files: list[str] = []
    names = _run_git(
        ["show", "--name-only", "--format=", commit_hash], project_root,
    )
    if names.returncode == 0:
        files = [ln for ln in names.stdout.splitlines() if ln.strip()]
    return {
        "hash": full_hash,
        "hash_short": full_hash[:12],
        "date": date_str,
        "author": author,
        "subject": subject,
        "type": classify_commit(subject),
        "fix_confidence": confidence,
        "crash_class": klass,
        "files_touched": len(files),
        "files": files[:40],
    }


def introduced_by(
    spec: str, project_root: Path, *, max_history: int = 25,
) -> dict:
    """Report which commit introduced a line, and the line's change history.

    Wraps ``git blame -L`` (who touched it last) and ``git log -L`` (every
    commit that ever rewrote that line range, oldest last). This is the
    primitive every fix-completeness verdict needs.
    """
    rel_path, start, end = parse_line_spec(spec)
    target = project_root / rel_path
    notes: list[str] = []
    report: dict = {
        "mode": "introduced-by",
        "project_root": str(project_root),
        "target": {"file": rel_path, "line_start": start, "line_end": end},
    }
    if not target.is_file():
        report["error"] = f"No such file under the project root: {rel_path}"
        report["notes"] = notes
        return report

    try:
        source = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        source = []
    report["line_text"] = source[start - 1] if 0 < start <= len(source) else ""

    # Who touched it last (git blame).
    blame = _run_git(
        ["blame", f"-L{start},{end}", "--porcelain", "--", rel_path],
        project_root,
    )
    last_touch: dict = {}
    if blame.returncode == 0 and blame.stdout.strip():
        head = blame.stdout.splitlines()[0].split()
        if head:
            last_touch = _commit_meta(head[0], project_root)
    else:
        notes.append(f"git blame failed for {rel_path}:{start} — line out of range?")
    report["last_touched_by"] = last_touch

    # Every commit that rewrote the range (git log -L), oldest last.
    log_l = _run_git(
        [
            "log", f"-L{start},{end}:{rel_path}", "-M",
            f"--max-count={max_history}", "--format=COMMIT:%H|%aI|%an|%s",
        ],
        project_root,
        timeout=_GIT_TIMEOUT * 2,
    )
    history: list[dict] = []
    if log_l.returncode == 0:
        current: dict | None = None
        hunk: list[str] = []
        for line in log_l.stdout.splitlines():
            if line.startswith("COMMIT:"):
                if current is not None:
                    current["hunk"] = "\n".join(hunk[:40])
                    history.append(current)
                    hunk = []
                parts = line[7:].split("|", 3)
                if len(parts) < 4:
                    current = None
                    continue
                commit_hash, date_str, author, subject = parts
                confidence, klass = score_fix(subject)
                current = {
                    "hash": commit_hash,
                    "hash_short": commit_hash[:12],
                    "date": date_str,
                    "author": author,
                    "subject": subject,
                    "type": classify_commit(subject),
                    "fix_confidence": confidence,
                    "crash_class": klass,
                }
            elif current is not None:
                hunk.append(line)
        if current is not None:
            current["hunk"] = "\n".join(hunk[:40])
            history.append(current)
    else:
        notes.append(
            "git log -L failed — the file may be new, binary, or outside the "
            "project root."
        )

    report["line_history"] = history
    report["introduced_by"] = (
        _commit_meta(history[-1]["hash"], project_root) if history else {}
    )
    if len(history) >= max_history:
        notes.append(
            f"Line history truncated at {max_history} commits; pass "
            "--max-commits to raise it."
        )
    notes.append(
        "`introduced_by` is the oldest commit `git log -L` reports for this "
        "range — it introduced the line in its current shape. A later "
        "`last_touched_by` means the line was rewritten since."
    )
    report["notes"] = notes
    return report


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

_INT_FLAGS: dict[str, str] = {
    "--days": "days",
    "--last": "last",
    "--max-commits": "max_commits",
    "--max-files": "max_files",
    "--workers": "workers",
    "--density-top": "density_top",
    "--density-days": "density_days",
}
_STR_FLAGS: dict[str, str] = {
    "--since": "since",
    "--until": "until",
    "--introduced-by": "introduced_by",
}
_BOOL_FLAGS: dict[str, str] = {
    "--no-function": "no_function",
    "--no-density": "no_density",
    "--no-follow": "no_follow",
}

KNOWN_FLAGS: tuple[str, ...] = tuple(
    sorted({*_INT_FLAGS, *_STR_FLAGS, *_BOOL_FLAGS})
)


def parse_args(argv: list[str]) -> dict:
    """Parse the CLI.

    Unrecognized ``-``-prefixed arguments are collected into
    ``unknown_args`` rather than dropped. Silently ignoring an unknown window
    flag (``--months 420`` when the flag is ``--days``) produced a confident,
    wrong temporal analysis at the default 90-day window.
    """
    args: dict = {
        "path": ".", "days": 90, "since": None, "until": None,
        "last": None, "max_commits": 50000, "max_files": 0,
        "workers": 8, "no_function": False, "no_density": False,
        "no_follow": False, "density_top": _DENSITY_TOP,
        "density_days": _DENSITY_WINDOW_DAYS, "introduced_by": None,
        "unknown_args": [],
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in _BOOL_FLAGS:
            args[_BOOL_FLAGS[arg]] = True
            i += 1
        elif arg in _INT_FLAGS or arg in _STR_FLAGS:
            if i + 1 >= len(argv):
                args["unknown_args"].append(f"{arg} (missing value)")
                i += 1
                continue
            value = argv[i + 1]
            if arg in _INT_FLAGS:
                try:
                    args[_INT_FLAGS[arg]] = int(value)
                except ValueError:
                    args["unknown_args"].append(
                        f"{arg} {value} (expected an integer)"
                    )
            else:
                args[_STR_FLAGS[arg]] = value
            i += 2
        elif arg.startswith("-"):
            args["unknown_args"].append(arg)
            i += 1
        else:
            args["path"] = arg
            i += 1
    return args


def analyze(argv: list[str] | None = None) -> dict:
    """Analyze git history for churn metrics and commit classification."""
    global _SCRIPT_START, _TIMEOUT_HIT
    _SCRIPT_START = time.monotonic()
    _TIMEOUT_HIT = False

    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)

    if args["unknown_args"]:
        unknown = ", ".join(args["unknown_args"])
        message = (
            f"Unrecognized argument(s): {unknown}. "
            f"Known options: {', '.join(KNOWN_FLAGS)}. "
            "Refusing to run — a silently-ignored window flag produces a "
            "confident but wrong temporal analysis."
        )
        print(f"analyze_history.py: error: {message}", file=sys.stderr)
        return {
            "error": message,
            "type": "UnknownArguments",
            "unknown_args": args["unknown_args"],
            "known_flags": list(KNOWN_FLAGS),
        }

    scan_root = Path(args["path"]).resolve()
    project_root = find_project_root(scan_root)

    if not _is_git_repo(project_root):
        return {
            "error": "Not a git repository",
            "project_root": str(project_root),
        }

    if args["introduced_by"]:
        try:
            return introduced_by(
                args["introduced_by"], project_root,
                max_history=min(args["max_commits"], 200),
            )
        except ValueError as exc:
            print(f"analyze_history.py: error: {exc}", file=sys.stderr)
            return {"error": str(exc), "type": "ValueError"}

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
        # Terminate git if it's still writing (parse_git_log may have
        # stopped reading before git finished, causing a pipe deadlock).
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        # Close the pipes explicitly; Popen is not context-managed here and
        # leaking them emits ResourceWarning into the toolkit's own stderr.
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

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
    commits_by_confidence: dict[str, int] = defaultdict(int)
    commits_by_crash_class: dict[str, int] = defaultdict(int)
    authors: set[str] = set()
    for c in commits:
        commits_by_type[c["type"]] += 1
        commits_by_confidence[c.get("fix_confidence", "none")] += 1
        if c.get("crash_class"):
            commits_by_crash_class[c["crash_class"]] += 1
        authors.add(c["author"])

    workers = args["workers"]

    function_churn: list[dict] = []
    function_churn_note: str | None = None
    if args["no_function"] or _check_script_timeout():
        function_churn_note = "Function-level churn skipped"
    else:
        function_churn = compute_function_churn(
            commits, scan_root, project_root,
            max_files=args["max_files"], workers=workers,
        )

    recent_fixes = get_commit_details(
        commits, "fix", project_root, scan_root, _MAX_DIFF_LINES_FIX,
        workers=workers,
    )
    recent_features = get_commit_details(
        commits, "feature", project_root, scan_root, _MAX_DIFF_LINES_FIX,
        workers=workers,
    )
    recent_refactors = get_commit_details(
        commits, "refactor", project_root, scan_root,
        _MAX_DIFF_LINES_REFACTOR, workers=workers,
    )

    co_change_clusters = compute_co_change_clusters(commits)

    # Annotate file churn with module family info.
    for fc in file_churn:
        family = get_module_family(fc["file"])
        if family:
            fc["module_family"] = family

    watchlist: list[dict] = []
    if not args["no_density"] and not _check_script_timeout():
        watchlist = compute_bugfix_density(
            file_churn, project_root,
            top_n=args["density_top"], window_days=args["density_days"],
            workers=workers, follow=not args["no_follow"],
        )

    shallow = is_shallow_clone(project_root)
    repo_commits, first_commit_date = _repo_depth(project_root)

    notes: list[str] = []
    if shallow:
        notes.append(
            "SHALLOW CLONE: this repository's history is truncated, so every "
            "temporal signal below (churn, fix density, watchlist, "
            "co-change) is unreliable and silently incomplete. Run "
            "`git fetch --unshallow` and re-run before trusting any of it."
        )
    if commit_cap_applied:
        notes.append(
            f"COMMIT CAP APPLIED: analysis stopped at --max-commits "
            f"{max_commits}; the window contains more commits than that, so "
            "everything below is a truncated prefix of the real history. "
            "Raise --max-commits or narrow the window."
        )
    if _TIMEOUT_HIT:
        notes.append(
            f"SCRIPT TIMEOUT ({_SCRIPT_TIMEOUT}s) reached: one or more passes "
            "(function churn, diff fetch, bug-fix density) stopped early and "
            "their output is partial. Narrow the scope or pass --no-function "
            "/ --no-density."
        )
    if function_churn_note:
        notes.append(
            "Function-level churn was skipped — `function_churn` is empty by "
            "request, not because nothing changed."
        )
    if args["since"] is None and args["last"] is None and args["days"] <= 365:
        notes.append(
            f"Window is {args['days']} days. CPython's C history goes back to "
            "1990; for fix-completeness or density work pass a much larger "
            "--days (the full history is ~13000 days)."
        )
    if watchlist:
        notes.append(
            "Prefer `watchlist` (recent crash-fix commits per KLOC) over "
            "`file_churn[].churn_rate` for prioritization: on CPython's "
            "Objects/ raw churn was anti-correlated with defect density."
        )

    result: dict = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "is_shallow_clone": shallow,
        "repo_total_commits": repo_commits,
        "repo_first_commit_date": first_commit_date,
        "timeout_hit": _TIMEOUT_HIT,
        "time_range": {
            "start": since,
            "end": until,
            "days": days,
            "commit_cap_applied": commit_cap_applied,
            "max_commits": max_commits,
        },
        "summary": {
            "total_commits": len(commits),
            "commits_by_type": dict(commits_by_type),
            "commits_by_fix_confidence": dict(commits_by_confidence),
            "commits_by_crash_class": dict(commits_by_crash_class),
            "files_changed": len(file_churn),
            "functions_changed": len(function_churn),
            "authors": len(authors),
        },
        "notes": notes,
        "watchlist": watchlist,
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

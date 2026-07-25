#!/usr/bin/env python3
"""Measure complexity metrics for C functions in CPython source files.

Outputs a JSON structure with per-function metrics:
- line_count, nesting_depth, cyclomatic_complexity
- parameter_count, local_variable_count, goto_count, switch_case_count
- manual_cleanup_ladder (goto-free manual cleanup burden -- see below)
- weighted score (1-10)

**Calibration warning (read before using ``score`` as a filter).** Measured on
CPython ``Objects/``: 3,073 functions, observed score range 1.0-6.5. An absolute
cutoff of 5.0 therefore fires ~3 times tree-wide and *zero* times on a typical
14-file sample, so the hotspot threshold here is **relative** (top N% by score,
``--top-percent``, default 2.0) with an optional absolute floor (``--min-score``).

The score **ranks** well and **gates** badly. Ground truth from a 14-file
``Objects/`` sample with 25 known defect-bearing functions: the top 10 by score
held 5 of the 25 (10x enrichment, p=0.00004), but 20 of the 25 sat at the score
floor. Never use ``score`` as a severity input -- for the recursion class it
actively *inverts* (``tuple_repr`` 1.6 vs the buggy ``tuple_hash`` 1.0), because
a recursion guard is itself a branch and so the *correct* twin outscores the
defective one.

``manual_cleanup_ladder`` is the experimental counter-metric:
``returns_with_cleanup * owned_locals``, reported only when ``goto_count == 0``.
In CPython a ``goto``-based cleanup ladder is a *positive* signal (24 of those
25 defect functions had zero gotos); its absence combined with several owned
locals released repeatedly before each ``return`` is the risk marker.

Usage:
    python measure_c_complexity.py [path] [--max-files N]
                                   [--top-percent P] [--min-score S]

    path: directory, file, or omitted for current directory
"""

import json
import math
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
    """Remove C comments and string/char literals from source.

    Line-preserving: a block comment is replaced by a space plus as many
    newlines as it spanned, so every surviving construct keeps its original
    line number. (The previous ``re.sub(r'/\\*.*?\\*/', ' ', ..., DOTALL)``
    collapsed multi-line comments to one space and silently shifted every
    subsequent line number in the file.)

    The literal patterns exclude newlines. A C string or character literal
    cannot contain a raw newline, and without that exclusion the double-quote
    pattern runs away at the first unbalanced quote -- which happens on the
    perfectly legal char literal ``'"'`` -- swallowing whole blocks of code.
    Measured on ``Objects/typeobject.c``: 3,210 of 13,068 lines (25%) were
    being consumed that way.
    """
    source = re.sub(
        r'/\*.*?\*/',
        lambda m: " " + "\n" * m.group(0).count("\n"),
        source,
        flags=re.DOTALL,
    )
    source = re.sub(r'//[^\n]*', ' ', source)
    source = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', source)
    source = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", source)
    return source


# ---------------------------------------------------------------------------
# C function detection
# ---------------------------------------------------------------------------

# Tokens that can never name a function definition.
_NOT_A_FUNCTION_NAME = frozenset({
    'if', 'for', 'while', 'switch', 'do', 'else', 'sizeof', 'return',
    'typedef', 'struct', 'union', 'enum', 'defined', 'case', 'goto',
})

# How many lines above the opening brace a signature may span. CPython's
# widest real signatures are ~8 lines; 16 is a safe ceiling that still stops
# a runaway backwards walk.
_MAX_SIGNATURE_LINES = 16

# A line that can never be part of the signature we are walking back through:
# statement terminators, block ends, preprocessor lines, labels.
_SIGNATURE_STOP_RE = re.compile(r'(?:[;}]|^\s*#)\s*$|^\s*#')


def _split_signature(text: str) -> tuple[str, str] | None:
    """Split a joined signature into ``(name, params)``.

    ``text`` must end with the parameter list's ``)``. The parameter list is
    located by matching that ``)`` backwards to its own ``(`` -- not by taking
    the *first* ``(`` -- so that a macro-wrapped return type such as
    ``Py_LOCAL_INLINE(int) foo(a, b)`` yields ``foo`` rather than
    ``Py_LOCAL_INLINE``.
    """
    text = text.strip()
    if not text.endswith(')'):
        return None
    depth = 0
    open_idx = -1
    for k in range(len(text) - 1, -1, -1):
        ch = text[k]
        if ch == ')':
            depth += 1
        elif ch == '(':
            depth -= 1
            if depth == 0:
                open_idx = k
                break
    if open_idx <= 0:
        return None
    head = text[:open_idx]
    m = re.search(r'(\w+)\s*$', head)
    if not m:
        return None
    name = m.group(1)
    if name in _NOT_A_FUNCTION_NAME:
        return None
    # A definition head is a declarator: type tokens, ``*``, and the name.
    # Anything else (``=``, ``,``, ``;``, ``&&`` ...) means this is an
    # expression or an initializer, not a function definition.
    if not re.fullmatch(r'[\w\s\*\)\(]*', head):
        return None
    params = text[open_idx + 1:-1].strip()
    return name, params


def find_functions(source: str) -> tuple[list[dict], dict]:
    """Find C function definitions and extract their bodies.

    Returns ``(functions, coverage)``. Each function dict has: name, params,
    body, start_line, end_line, signature_lines.

    The signature may span any number of lines up to ``_MAX_SIGNATURE_LINES``:
    the walk backwards from the opening brace accumulates lines until the
    parentheses balance. (The previous implementation matched only the single
    line directly above the brace, silently dropping every multi-line
    parameter list -- 22.4% of functions on a measured CPython sample.)

    Comments are stripped first (line-preservingly), and blank lines are
    skipped during the walk, so an Argument Clinic ``_impl`` function -- whose
    signature is separated from its brace by a ``/*[clinic end generated
    code: ...]*/`` block -- is found. That shape alone accounted for 305 of the
    324 misses on CPython ``Objects/``.

    ``coverage`` reports how many column-0 opening braces were seen and how
    many yielded a function, so a future regression in signature parsing is
    visible in the output envelope instead of silent.
    """
    lines = strip_comments_and_strings(source).split('\n')
    functions: list[dict] = []
    brace_blocks = 0
    unparsed: list[int] = []
    multiline_signatures = 0

    # Strategy: find opening braces at column 0, then work backwards
    # to find the function signature.
    for i, line in enumerate(lines):
        if not line.startswith('{'):
            continue
        if i < 1:
            continue
        brace_blocks += 1

        # Walk backwards accumulating signature lines until parens balance.
        parsed: tuple[str, str] | None = None
        sig_first = i - 1
        chunk: list[str] = []
        for j in range(i - 1, max(-1, i - 1 - _MAX_SIGNATURE_LINES), -1):
            stripped = lines[j].strip()
            if not stripped:
                # Blank (or comment-only, now stripped) line: skip it. This is
                # what lets the Argument Clinic `_impl` shape resolve.
                continue
            if chunk and _SIGNATURE_STOP_RE.search(stripped):
                break
            chunk.insert(0, stripped)
            joined = ' '.join(chunk)
            if joined.count('(') == joined.count(')') and '(' in joined:
                parsed = _split_signature(joined)
                sig_first = j
                break
            if _SIGNATURE_STOP_RE.search(stripped):
                break
        if parsed is None:
            unparsed.append(i + 1)
            continue
        func_name, params = parsed
        if len(chunk) > 1:
            multiline_signatures += 1

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
        # Determine start line (return type might be on the line above).
        sig_start = sig_first
        if sig_start > 0 and re.match(r'^[\w\s\*]+$', lines[sig_start - 1].strip()):
            # Previous line looks like a return type.
            sig_start -= 1

        functions.append({
            "name": func_name,
            "params": params,
            "body": body,
            "start_line": sig_start + 1,  # 1-indexed
            "end_line": body_end + 1,
            "signature_lines": len(chunk),
        })

    coverage = {
        "brace_blocks_seen": brace_blocks,
        "functions_parsed": len(functions),
        "signatures_unparsed": len(unparsed),
        "multiline_signatures": multiline_signatures,
        "coverage_pct": (
            round(100.0 * len(functions) / brace_blocks, 1) if brace_blocks else 100.0
        ),
    }
    return functions, coverage


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

# ---------------------------------------------------------------------------
# manual_cleanup_ladder (experimental counter-metric -- see module docstring)
# ---------------------------------------------------------------------------

# Calls that release an owned resource. A local that is passed to one of these
# is an *owned* local: the function is responsible for freeing it.
_RELEASE_APIS = (
    'Py_DECREF', 'Py_XDECREF', 'Py_CLEAR', 'Py_SETREF', 'Py_XSETREF',
    'PyMem_Free', 'PyMem_RawFree', 'PyMem_Del', 'PyObject_Free',
    'PyObject_GC_Del', 'PyBuffer_Release', 'free',
)
_RELEASE_CALL = re.compile(
    r'\b(?:' + '|'.join(_RELEASE_APIS) + r')\s*\(\s*&?\s*([A-Za-z_]\w*)'
)
_RELEASE_ANY = re.compile(r'\b(?:' + '|'.join(_RELEASE_APIS) + r')\s*\(')

# A local pointer declaration: ``PyObject *foo``, ``char *buf = NULL``, ...
_POINTER_LOCAL = re.compile(
    r'^\s+(?:(?:static|const|volatile|register|unsigned|signed)\s+)*'
    r'(?:struct\s+)?\w+\s*\*+\s*([A-Za-z_]\w*)\s*(?:[=;,)]|\[)',
    re.MULTILINE,
)
_RETURN_STMT = re.compile(r'^\s*return\b')

# Walking back from a `return`, these end the window: another exit, or a block
# boundary. Without this the cleanup belonging to the *previous* return leaks
# into the next one's window and inflates the count.
_CLEANUP_WINDOW_STOP = re.compile(r'^\s*[}{]|\breturn\b|\bgoto\b|^\s*\w+\s*:\s*$')

# How many preceding non-blank lines count as "the cleanup before this return".
_CLEANUP_WINDOW = 3


def measure_cleanup_ladder(clean_body: str) -> dict:
    """Measure the goto-free manual-cleanup burden of a function body.

    Returns ``owned_locals``, ``returns_with_cleanup`` and their product
    ``manual_cleanup_ladder``.

    * ``owned_locals`` -- local pointer declarations whose name is later
      passed to a release API (``Py_DECREF`` / ``PyMem_Free`` / ``free`` ...),
      i.e. locals this function must dispose of itself.
    * ``returns_with_cleanup`` -- ``return`` statements with at least one
      release call in the preceding ``_CLEANUP_WINDOW`` non-blank lines.

    Their product approximates "how many times the same cleanup sequence had
    to be hand-written". A ``goto``-based ladder writes it once, which is why
    the caller reports the product only when ``goto_count == 0``.
    """
    declared = set(_POINTER_LOCAL.findall(clean_body))
    released = set(_RELEASE_CALL.findall(clean_body))
    owned_locals = len(declared & released)

    body_lines = clean_body.split('\n')
    returns_with_cleanup = 0
    for idx, line in enumerate(body_lines):
        if not _RETURN_STMT.match(line):
            continue
        seen = 0
        for back in range(idx - 1, -1, -1):
            prev = body_lines[back]
            if not prev.strip():
                continue
            seen += 1
            if _RELEASE_ANY.search(prev):
                returns_with_cleanup += 1
                break
            if _CLEANUP_WINDOW_STOP.search(prev):
                break
            if seen >= _CLEANUP_WINDOW:
                break
    return {
        "owned_locals": owned_locals,
        "returns_with_cleanup": returns_with_cleanup,
        "manual_cleanup_ladder": owned_locals * returns_with_cleanup,
    }


def _count_top_level_params(params: str) -> int:
    """Number of parameters, counting only commas at paren depth 0.

    A naive ``params.count(',') + 1`` counts the commas *inside* a
    function-pointer parameter's own argument list. ``do_lookup``
    (Objects/dictobject.c) takes 5 parameters and was reported as having 10 —
    4 scalars plus the 6 types inside ``int (*compare)(PyDictObject *,
    PyDictKeysObject *, void *, Py_ssize_t, PyObject *, Py_hash_t)`` — which
    pushed it over the ``param_count > 6`` scoring threshold and put it in the
    hotspot list on an artifact. Corrected, it scores 1.0 and drops off.
    """
    depth = 0
    count = 1
    for ch in params:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count


def measure_function(func: dict) -> dict:
    """Compute complexity metrics for a single C function."""
    body = func["body"]
    clean = strip_comments_and_strings(body)
    body_lines = [l for l in clean.split('\n') if l.strip()]
    line_count = len(body_lines)

    # Parameter count.
    params = func["params"].strip()
    if params and params != "void":
        param_count = _count_top_level_params(params)
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

    ladder = measure_cleanup_ladder(clean)

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
        "signature_lines": func.get("signature_lines", 1),
        "owned_locals": ladder["owned_locals"],
        "returns_with_cleanup": ladder["returns_with_cleanup"],
        # Only meaningful without a goto ladder -- see the module docstring.
        "manual_cleanup_ladder": (
            ladder["manual_cleanup_ladder"] if goto_count == 0 else 0
        ),
        "score": score,
    }


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

DEFAULT_TOP_PERCENT = 2.0
_MAX_HOTSPOTS = 50


def select_hotspots(
    all_funcs: list[dict],
    *,
    top_percent: float = DEFAULT_TOP_PERCENT,
    min_score: float = 0.0,
) -> tuple[list[dict], float]:
    """Select hotspots by *relative* rank, returning ``(hotspots, threshold)``.

    The shipped absolute cutoff (``score >= 5.0``) fired 3 times across all of
    CPython ``Objects/`` -- whose maximum observed score is 6.5 -- and zero
    times on a 14-file sample. Ranking by percentile keeps the check meaningful
    on any corpus. ``min_score`` layers an optional absolute floor on top.

    Ties at the cut score are kept, so the returned list may be slightly longer
    than ``top_percent`` of the corpus. A function sitting at the corpus
    *minimum* is never a hotspot, however: on a flat distribution the
    percentile cut can land on the floor itself, which would otherwise select
    the whole corpus.
    """
    if not all_funcs:
        return [], min_score
    ranked = sorted(all_funcs, key=lambda x: -x["score"])
    floor = ranked[-1]["score"]
    cut = max(1, math.ceil(len(ranked) * top_percent / 100.0))
    threshold = max(ranked[min(cut, len(ranked)) - 1]["score"], min_score)
    if threshold <= floor:
        selected = [
            f for f in ranked if f["score"] > floor and f["score"] >= min_score
        ]
    else:
        selected = [f for f in ranked if f["score"] >= threshold]
    effective = min((f["score"] for f in selected), default=threshold)
    return selected, effective


def analyze(
    target: str,
    *,
    max_files: int = 0,
    top_percent: float = DEFAULT_TOP_PERCENT,
    min_score: float = 0.0,
) -> dict:
    """Analyze C function complexity for the given target path."""
    target_path = Path(target).resolve()
    project_root = find_cpython_root(target_path)
    if project_root is None:
        project_root = target_path if target_path.is_dir() else target_path.parent
    # scan_root is the target itself: discover_c_files() yields just that file
    # for a file root. Using .parent here silently widened a single-file scan
    # to the entire directory.
    scan_root = target_path

    files_data: list[dict] = []
    total_functions = 0
    files_analyzed = 0
    cov_totals = {
        "brace_blocks_seen": 0,
        "functions_parsed": 0,
        "signatures_unparsed": 0,
        "multiline_signatures": 0,
    }

    for filepath in discover_c_files(scan_root, max_files=max_files):
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files_analyzed += 1

        functions, coverage = find_functions(source)
        for key in cov_totals:
            cov_totals[key] += coverage[key]
        if not functions:
            continue

        rel = str(filepath.relative_to(project_root))
        file_entry: dict = {"file": rel, "functions": []}

        for func in functions:
            metrics = measure_function(func)
            total_functions += 1
            file_entry["functions"].append(metrics)

        if file_entry["functions"]:
            files_data.append(file_entry)

    # Collect all functions and sort by score descending.
    all_funcs = []
    for f in files_data:
        for fn in f["functions"]:
            all_funcs.append({**fn, "file": f["file"]})
    all_funcs.sort(key=lambda x: -x["score"])

    hotspots, threshold = select_hotspots(
        all_funcs, top_percent=top_percent, min_score=min_score
    )
    ladder_ranked = sorted(
        (f for f in all_funcs if f["manual_cleanup_ladder"] > 0),
        key=lambda x: -x["manual_cleanup_ladder"],
    )

    # The percentile is computed over the MERGED corpus, so in a multi-file run
    # a smaller file can be squeezed out entirely and report nothing — which
    # reads as "clean here" when it means "outranked elsewhere". Running over
    # Objects/dictobject.c + setobject.c together gave setobject.c zero
    # hotspots. Silent truncation is the same failure mode as a zero
    # denominator, so name the files that contributed none.
    # Only meaningful when something DID make the cut: if the whole corpus
    # produced no hotspots, no file was outranked by another.
    hotspot_files = {h.get("file") for h in hotspots}
    starved = (
        sorted(
            f["file"]
            for f in files_data
            if f.get("functions") and f["file"] not in hotspot_files
        )
        if hotspots
        else []
    )

    seen = cov_totals["brace_blocks_seen"]
    coverage = {
        **cov_totals,
        "coverage_pct": (
            round(100.0 * cov_totals["functions_parsed"] / seen, 1) if seen else 100.0
        ),
    }

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_analyzed": files_analyzed,
        "functions_analyzed": total_functions,
        "coverage": coverage,
        "files": files_data,
        "hotspots": hotspots[:_MAX_HOTSPOTS],
        "cleanup_ladders": ladder_ranked[:_MAX_HOTSPOTS],
        "summary": {
            "total_functions": total_functions,
            "hotspot_count": len(hotspots),
            "hotspot_threshold": threshold,
            "hotspot_selection": (
                f"top {top_percent}% by score"
                + (f", floor {min_score}" if min_score else "")
            ),
            "files_without_hotspots": starved,
            "files_without_hotspots_note": (
                "The percentile is computed over the merged corpus, so these "
                "files have functions but contributed no hotspot: they were "
                "outranked, NOT shown to be simple. Re-run per file before "
                "reading any of them as clean."
            )
            if starved
            else "",
            "max_score": all_funcs[0]["score"] if all_funcs else 0.0,
            "max_cleanup_ladder": (
                ladder_ranked[0]["manual_cleanup_ladder"] if ladder_ranked else 0
            ),
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
            "signal_caveat": (
                "score RANKS well but GATES badly: on a measured CPython sample "
                "the top 10 held 5 of 25 defect-bearing functions (10x "
                "enrichment) while 20 of 25 sat at the score floor. Do not use "
                "score as a severity input, and never for recursion/free-"
                "threading findings, where it inverts."
            ),
        },
    }


def main() -> None:
    max_files = 0
    top_percent = DEFAULT_TOP_PERCENT
    min_score = 0.0
    positional: list[str] = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--max-files" and i + 1 < len(argv):
            max_files = int(argv[i + 1])
            i += 2
        elif argv[i] == "--top-percent" and i + 1 < len(argv):
            top_percent = float(argv[i + 1])
            i += 2
        elif argv[i] == "--min-score" and i + 1 < len(argv):
            min_score = float(argv[i + 1])
            i += 2
        elif argv[i].startswith("--"):
            i += 1
        else:
            positional.append(argv[i])
            i += 1
    target = positional[0] if positional else "."
    result = analyze(
        target, max_files=max_files, top_percent=top_percent, min_score=min_score
    )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

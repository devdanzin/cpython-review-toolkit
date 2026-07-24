#!/usr/bin/env python3
"""Scan CPython C source for GIL discipline issues.

Detects mismatched ``Py_BEGIN``/``END_ALLOW_THREADS``, Python C-API calls made
inside a GIL-released region, blocking system calls made with the GIL held, and
``PyGILState_Ensure`` / ``Release`` imbalance.

The constructs live almost entirely in ``Modules/`` (``_ssl``, ``_io``,
``posixmodule``, ``select``, ``socket``). ``Objects/`` contains zero
``PyGILState_*`` and only a handful of ``ALLOW_THREADS`` pairs, so a zero
result there means "no constructs present", not "constructs present and clean"
— the ``vocabulary_counts`` field in the envelope carries the denominator that
tells the two apart.

This scanner used to carry a private regex function-finder that required an
opening brace in column 0 preceded by a signature-shaped line. Measured against
the shared tree-sitter chassis on ``Objects/``, it covered 52% of lines vs 69%
and left **33.2% of all ``return`` statements outside any extracted function**.
It now uses ``tree_sitter_utils.extract_functions()`` like every other scanner,
which deletes code rather than adding a dependency: tree-sitter was already a
hard requirement of the toolkit.

Usage:
    python scan_gil_usage.py [path] [--max-files N]

    path: directory, file, or omitted for current directory
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_common import (
    build_report,
    discover_c_files,
    parse_common_args,
    relpath,
    resolve_roots,
)
from tree_sitter_utils import (
    extract_functions,
    parse_bytes,
)

# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------

# One left-to-right pass over comments and literals. Running the four patterns
# as separate `re.sub` passes is wrong: a character literal holding a quote —
# `'"'`, which `Modules/socketmodule.c` contains — leaves a lone `"` behind
# once comments are gone, and the string pattern then swallows everything up to
# the next quote. That silently deleted 40+ KB of socketmodule.c, including
# every reference to `sock_recv_impl`. Alternation fixes it because the char
# literal is consumed as a char literal.
_MASK_RE = re.compile(
    r"/\*.*?\*/"  # block comment
    r"|//[^\n]*"  # line comment
    r'|"(?:[^"\\\n]|\\.)*"'  # string literal (cannot span a raw newline)
    r"|'(?:[^'\\\n]|\\.)*'",  # character literal
    re.DOTALL,
)


def _blank(match: re.Match) -> str:
    """Same length, same newlines — so every byte offset and line survives."""
    text = match.group(0)
    newlines = text.count("\n")
    return " " * (len(text) - newlines) + "\n" * newlines


def strip_comments_and_strings(source: str) -> str:
    """Blank out comments and literals, preserving length and line structure.

    Every offset computed on the result maps back to the same line in the
    original. The previous implementation replaced a whole block comment with a
    single space, so a five-line comment shifted every finding after it up by
    four lines; combined with the +1 the caller added, reported lines drifted by
    anything from -3 to +16. A docstring mentioning ``read()`` also used to be
    reported as a blocking call (``Objects/iterobject.c:465``).
    """
    return _MASK_RE.sub(_blank, source)


# ---------------------------------------------------------------------------
# GIL macros and patterns
# ---------------------------------------------------------------------------

_BEGIN_ALLOW = re.compile(r"\bPy_BEGIN_ALLOW_THREADS\b")
_END_ALLOW = re.compile(r"\bPy_END_ALLOW_THREADS\b")
_GILSTATE_ENSURE = re.compile(r"\bPyGILState_Ensure\b")
_GILSTATE_RELEASE = re.compile(r"\bPyGILState_Release\b")

# Python C API calls that require the GIL.
_PYTHON_API_RE = re.compile(
    r"\b(Py_(?:INCREF|DECREF|XINCREF|XDECREF|CLEAR|SETREF|BuildValue|"
    r"Initialize|Finalize)|"
    r"Py[A-Z]\w+|"
    r"_Py[A-Z]\w+)\s*\("
)

# A GIL-released region may legitimately call these: they are the primitives
# that manage the released state itself, or are documented GIL-free.
_GIL_FREE_API = frozenset(
    {
        "Py_BEGIN_ALLOW_THREADS",
        "Py_END_ALLOW_THREADS",
        "Py_BLOCK_THREADS",
        "Py_UNBLOCK_THREADS",
        "PyEval_RestoreThread",
        "PyEval_SaveThread",
        "PyMem_RawMalloc",
        "PyMem_RawCalloc",
        "PyMem_RawRealloc",
        "PyMem_RawFree",
        "PyMutex_Lock",
        "PyMutex_Unlock",
        "PyThread_acquire_lock",
        "PyThread_acquire_lock_timed",
        "PyThread_release_lock",
        "PyThread_get_thread_ident",
        "PyThread_allocate_lock",
        "PyThread_free_lock",
    }
)

# Blocking I/O calls.
BLOCKING_CALLS = frozenset(
    {
        "read",
        "write",
        "recv",
        "send",
        "recvfrom",
        "sendto",
        "sleep",
        "usleep",
        "nanosleep",
        "select",
        "poll",
        "epoll_wait",
        "kevent",
        "flock",
        "lockf",
        "fcntl",
        "connect",
        "accept",
        "listen",
        "bind",
        "waitpid",
        "wait",
        "waitid",
        "fgets",
        "fread",
        "fwrite",
        "fflush",
        "getaddrinfo",
        "gethostbyname",
        "popen",
        "system",
        "pthread_mutex_lock",
        "pthread_cond_wait",
        "sem_wait",
    }
)

_BLOCKING_RE = re.compile(
    r"\b(" + "|".join(re.escape(c) for c in sorted(BLOCKING_CALLS)) + r")\s*\("
)

# Vocabulary whose raw per-scope count is reported, so a zero finding count has
# a denominator. See the module docstring.
_VOCABULARY = {
    "Py_BEGIN_ALLOW_THREADS": _BEGIN_ALLOW,
    "Py_END_ALLOW_THREADS": _END_ALLOW,
    "PyGILState_Ensure": _GILSTATE_ENSURE,
    "PyGILState_Release": _GILSTATE_RELEASE,
}


# ---------------------------------------------------------------------------
# GIL analysis
# ---------------------------------------------------------------------------


def analyze_function_gil(
    func: dict,
    local_names: frozenset[str] = frozenset(),
    callback_names: frozenset[str] = frozenset(),
) -> list[dict]:
    """Analyze GIL discipline in a single function.

    Offsets in the returned findings are ``line_offset``: 0-based line counts
    from the start of the function *body*, which the caller adds to the body's
    opening-brace line.

    ``local_names`` is the set of functions and macros the enclosing file
    defines itself. The ``_Py[A-Z]\\w+`` half of the API pattern otherwise
    matches module-private helpers — ``_PySSL_errno`` (``Modules/_ssl.c``) is
    called inside every one of that file's ``Py_BEGIN_ALLOW_THREADS`` regions
    on purpose, and accounted for 6 of this scanner's ``Modules/`` findings.
    """
    clean = strip_comments_and_strings(func["body"])
    is_callback = func["name"] in callback_names
    findings: list[dict] = []

    # 1. Check BEGIN/END_ALLOW_THREADS balance.
    begins = list(_BEGIN_ALLOW.finditer(clean))
    ends = list(_END_ALLOW.finditer(clean))
    if len(begins) != len(ends):
        findings.append(
            {
                "type": "mismatched_allow_threads",
                "line_offset": (
                    clean[: begins[0].start()].count("\n") if begins else 0
                ),
                "detail": (
                    f"Mismatched Py_BEGIN_ALLOW_THREADS ({len(begins)}) / "
                    f"Py_END_ALLOW_THREADS ({len(ends)}) in '{func['name']}' — "
                    "the GIL is released on some path and never reacquired (or "
                    "vice versa)."
                ),
                "confidence": "high",
            }
        )

    # 2. Check for Python API calls within GIL-released regions.
    for bi, begin in enumerate(begins):
        end_pos = ends[bi].start() if bi < len(ends) else len(clean)
        region = clean[begin.end() : end_pos]
        region_offset = clean[: begin.start()].count("\n")

        for api_m in _PYTHON_API_RE.finditer(region):
            api_name = api_m.group(1)
            if (
                api_name in _GIL_FREE_API
                or api_name in local_names
                or api_name.startswith("Py_UNREACHABLE")
            ):
                continue
            line_offset = region_offset + region[: api_m.start()].count("\n")
            findings.append(
                {
                    "type": "api_without_gil",
                    "api_call": api_name,
                    "line_offset": line_offset,
                    "detail": (
                        f"Python C API call {api_name}() within "
                        f"Py_BEGIN_ALLOW_THREADS region of '{func['name']}' "
                        "(GIL not held)"
                    ),
                    "confidence": "high",
                }
            )

    # 3. Check for blocking calls without GIL release.
    #
    # Skipped entirely for a function the file hands to someone else as a
    # callback: CPython's socket module releases the GIL in `sock_call_ex`
    # (Modules/socketmodule.c:1044) and passes `sock_recv_impl` &co in, so the
    # blocking call really is made with the GIL released — one frame up, where
    # an intra-function scanner cannot see it. That single pattern accounted
    # for 10 of the 16 findings in _ssl/_io/posix/socket.
    for bm in _BLOCKING_RE.finditer(clean) if not is_callback else ():
        call_name = bm.group(1)
        call_pos = bm.start()
        in_released = False
        for bi, begin in enumerate(begins):
            end_pos = ends[bi].start() if bi < len(ends) else len(clean)
            if begin.end() <= call_pos <= end_pos:
                in_released = True
                break
        if not in_released:
            findings.append(
                {
                    "type": "blocking_with_gil",
                    "api_call": call_name,
                    "line_offset": clean[:call_pos].count("\n"),
                    "detail": (
                        f"Blocking call {call_name}() in '{func['name']}' "
                        "without releasing the GIL (no Py_BEGIN_ALLOW_THREADS)"
                    ),
                    "confidence": "medium",
                }
            )

    # 4. Check PyGILState_Ensure / Release balance.
    #
    # Directional on purpose. `Ensure` once and `Release` on each of N exit
    # paths is the canonical callback idiom — every one of `Modules/_ssl.c`'s
    # four callbacks does it, and a symmetric count test reported all four as
    # bugs. Only *fewer* releases than ensures can leave the GIL held.
    ensures = list(_GILSTATE_ENSURE.finditer(clean))
    releases = list(_GILSTATE_RELEASE.finditer(clean))
    if len(ensures) > len(releases):
        findings.append(
            {
                "type": "mismatched_gilstate",
                "line_offset": clean[: ensures[0].start()].count("\n"),
                "detail": (
                    f"PyGILState_Ensure ({len(ensures)}) outnumbers "
                    f"PyGILState_Release ({len(releases)}) in '{func['name']}' "
                    "— at least one path returns with the GIL still held."
                ),
                "confidence": "high",
            }
        )

    return findings


_DEFINE_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)", re.MULTILINE)


def _local_definitions(source: str, functions: list[dict]) -> frozenset[str]:
    """Functions and macros this file defines itself."""
    names = {f["name"] for f in functions}
    names.update(_DEFINE_RE.findall(source))
    return frozenset(names)


def _callback_definitions(source: str, functions: list[dict]) -> frozenset[str]:
    """File-local functions passed elsewhere as a bare function pointer.

    Only meaningful when the file releases the GIL somewhere: the caller that
    receives the pointer is where the ``Py_BEGIN_ALLOW_THREADS`` lives.
    """
    if "Py_BEGIN_ALLOW_THREADS" not in source:
        return frozenset()
    found = set()
    for func in functions:
        name = func["name"]
        if re.search(rf"[(,]\s*{re.escape(name)}\s*[,)]", source):
            found.add(name)
    return frozenset(found)


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Analyze GIL discipline for the given target path."""
    project_root, scan_root = resolve_roots(target)

    all_findings: list[dict] = []
    functions_analyzed = 0
    files_analyzed = 0
    skipped: list[dict] = []
    vocab_counts: dict[str, int] = dict.fromkeys(_VOCABULARY, 0)

    for filepath in discover_c_files(scan_root, max_files=max_files):
        files_analyzed += 1
        try:
            source_bytes = filepath.read_bytes()
        except OSError as e:
            skipped.append({"file": str(filepath), "reason": str(e)})
            continue

        try:
            tree = parse_bytes(source_bytes)
        except Exception as e:  # pragma: no cover - defensive
            skipped.append({"file": str(filepath), "reason": f"parse: {e}"})
            continue

        source = strip_comments_and_strings(
            source_bytes.decode("utf-8", errors="replace")
        )
        for name, pattern in _VOCABULARY.items():
            vocab_counts[name] += len(pattern.findall(source))

        rel = relpath(filepath, project_root)
        functions = extract_functions(tree, source_bytes)
        local_names = _local_definitions(source, functions)
        callback_names = _callback_definitions(source, functions)
        for func in functions:
            functions_analyzed += 1
            # The body's opening brace, not the function_definition start:
            # CPython puts the return type on its own line above the signature.
            body_line = func["body_node"].start_point[0] + 1
            for finding in analyze_function_gil(func, local_names, callback_names):
                finding["file"] = rel
                finding["function"] = func["name"]
                finding["line"] = body_line + finding.pop("line_offset")
                all_findings.append(finding)

    by_type: dict[str, int] = defaultdict(int)
    for f in all_findings:
        by_type[f["type"]] += 1

    return build_report(
        project_root=project_root,
        scan_root=scan_root,
        files_analyzed=files_analyzed,
        functions_analyzed=functions_analyzed,
        findings=all_findings,
        summary={
            "mismatched_pairs": by_type["mismatched_allow_threads"]
            + by_type["mismatched_gilstate"],
            "api_without_gil": by_type["api_without_gil"],
            "blocking_with_gil": by_type["blocking_with_gil"],
            "total_findings": len(all_findings),
            "by_type": dict(by_type),
        },
        vocabulary_counts=vocab_counts,
        skipped_files=skipped,
    )


def main() -> None:
    try:
        target, max_files = parse_common_args(sys.argv[1:])
        result = analyze(target, max_files=max_files)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:  # noqa: BLE001
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()

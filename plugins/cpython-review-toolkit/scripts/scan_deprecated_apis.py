#!/usr/bin/env python3
"""Scan CPython C source for use of its own deprecated C APIs.

Replaces the api-deprecation-tracker's hard-coded 2021 pattern list, which
scored 0 real hits out of 13 on ``Objects/``: every match was the deprecated
API's own definition site, comment prose, or a substring false positive
(``PyUnicode_AsUnicode`` matching the live ``PyUnicode_AsUnicodeEscapeString``).

What changed:

* **Data-driven.** The vocabulary lives in ``data/deprecated_c_apis.json``,
  every entry verified against a CPython checkout (``Py_DEPRECATED`` markers in
  ``Include/``, ``.. deprecated::`` directives in ``Doc/c-api/``, and
  ``Doc/deprecations/``). Entries carry ``deprecated_in`` / ``removed_in`` /
  ``replacement``, so the report is a migration worklist, not a name dump.
* **Word boundaries.** ``\\bNAME\\b`` -- no more substring matches.
* **Definition-site suppression.** A match on a declaration
  (``PyAPI_FUNC`` / ``Py_DEPRECATED`` / ``#define NAME``), on the symbol's own
  column-0 definition header, or anywhere inside that definition's body, is
  not a use.
* **Comments and string literals stripped first** (line-preservingly), which
  is what removes the Argument Clinic docstring prose that made ``Py_UNICODE``
  look used in ``Modules/``.

The highest-value tier is ``hard-internal``: APIs marked with
``_Py_DEPRECATED_EXTERNALLY``, which ``Include/pyport.h:269-274`` expands to
*nothing* under ``Py_BUILD_CORE``. The compiler never warns about CPython's own
calls to those, so a scanner is the only thing that finds them.

Also emits one safety rule that is not about deprecation at all:

``gc-untrack-macro-form``
    ``_PyObject_GC_UNTRACK`` (internal macro, asserts the object *is* tracked)
    used in a ``tp_dealloc`` whose constructor has a **pre-track free path** --
    a ``Py_DECREF`` of the new object between ``PyObject_GC_New`` and
    ``_PyObject_GC_TRACK``. On that path dealloc runs on an untracked object
    and the assertion turns a recoverable allocation failure into a SIGABRT.
    The exemplar is ``Objects/odictobject.c``: ``odictiter_new`` allocates,
    then ``Py_DECREF(di)`` if ``_PyTuple_FromPairSteal`` fails, and
    ``odictiter_dealloc`` untracks with the strict macro -- while the tolerant
    twin ``odict_dealloc`` in the same file uses ``PyObject_GC_UnTrack``.

    Note ``PyObject_GC_UnTrack`` is **untracked-tolerant, not NULL-safe**:
    ``_PyObject_GC_IS_TRACKED`` dereferences unconditionally, so it still
    segfaults on NULL. Do not describe it as a ``Py_XDECREF``-style guard.

Usage:
    python scan_deprecated_apis.py [path] [--max-files N]
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

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_deprecated_apis(path: Path | None = None) -> list[dict]:
    """Load the deprecated-API vocabulary from ``data/deprecated_c_apis.json``."""
    target = path or (_DATA_DIR / "deprecated_c_apis.json")
    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return [a for a in data.get("apis", []) if a.get("name")]


# ---------------------------------------------------------------------------
# Line-preserving comment / string removal (stdlib only, exact line numbers)
# ---------------------------------------------------------------------------


def strip_comments_and_strings(source: str) -> str:
    """Blank out comments and literals while preserving every line number.

    A block comment becomes a space plus its own newlines, so a match's line
    index is always the real one. The literal patterns exclude newlines: a C
    string cannot span a raw newline, and without that exclusion the
    double-quote pattern runs away at the first lone quote (the legal char
    literal ``'"'`` is enough to trigger it) and swallows whole functions.
    """
    source = re.sub(
        r"/\*.*?\*/",
        lambda m: " " + "\n" * m.group(0).count("\n"),
        source,
        flags=re.DOTALL,
    )
    source = re.sub(r"//[^\n]*", " ", source)
    source = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', source)
    source = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", source)
    return source


# ---------------------------------------------------------------------------
# Definition-site detection
# ---------------------------------------------------------------------------

# A line that declares rather than uses the symbol.
_DECLARATION_MARKERS = (
    "PyAPI_FUNC",
    "PyAPI_DATA",
    "Py_DEPRECATED",
    "_Py_DEPRECATED_EXTERNALLY",
    "PyAPI_",
)


def _is_declaration_line(line: str, name: str) -> bool:
    """True if ``line`` declares ``name`` instead of calling it."""
    if any(marker in line for marker in _DECLARATION_MARKERS):
        return True
    if re.match(rf"^\s*#\s*(?:define|undef)\s+{re.escape(name)}\b", line):
        return True
    if re.match(r"^\s*extern\b", line):
        return True
    # Forward declaration: `static PyObject *name(args);`
    #
    # This must NOT swallow a statement-form call -- `_PyUnicodeWriter_Init(&w);`
    # has exactly the same tail. The discriminator is what precedes the name: a
    # declaration carries a return type, a call statement carries nothing (or
    # `return` / an assignment, which is not a plain type token sequence).
    m = re.search(rf"(?:^|[^\w])({re.escape(name)})\s*\([^;]*\)\s*;\s*$", line)
    if not m:
        return False
    head = line[: m.start(1)].strip()
    if not head or not re.fullmatch(r"[\w\s\*]+", head):
        return False
    return not re.match(r"^(?:return|else|do)\b", head)


def find_definition_ranges(lines: list[str], name: str) -> list[tuple[int, int]]:
    """Return 0-indexed ``[start, end]`` line ranges defining ``name``.

    Matches CPython's layout: the definition header sits at column 0 as
    ``name(`` and the body ends at the next column-0 ``}``. A match anywhere
    inside such a range is the definition, not a use of the API.
    """
    ranges: list[tuple[int, int]] = []
    header = re.compile(rf"^{re.escape(name)}\s*\(")
    for i, line in enumerate(lines):
        if not header.match(line):
            continue
        end = len(lines) - 1
        for j in range(i + 1, len(lines)):
            if lines[j].startswith("}"):
                end = j
                break
        # Include the return-type line above the header.
        start = i - 1 if i > 0 and re.match(r"^[\w\s\*]+$", lines[i - 1]) else i
        ranges.append((start, end))
    return ranges


def _in_ranges(index: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= index <= end for start, end in ranges)


# ---------------------------------------------------------------------------
# gc-untrack-macro-form
# ---------------------------------------------------------------------------

_GC_NEW_RE = re.compile(
    r"\b(?:PyObject_GC_New|PyObject_GC_NewVar|_PyObject_GC_New"
    r"|_PyObject_GC_NewVar|PyObject_GC_Resize)\s*\(\s*\w+\s*,\s*&?\s*(\w+)"
)
_GC_NEW_ASSIGN_RE = re.compile(
    r"(\w+)\s*=\s*(?:\([^)]*\)\s*)?"
    r"(?:PyObject_GC_New|PyObject_GC_NewVar|_PyObject_GC_New|_PyObject_GC_NewVar)\s*\("
)
_GC_TRACK_RE = re.compile(r"\b(?:_PyObject_GC_TRACK|PyObject_GC_Track)\s*\(\s*\(?\s*(\w+)")
_UNTRACK_MACRO_RE = re.compile(r"\b_PyObject_GC_UNTRACK\s*\(")
_FREE_CALL_RE = re.compile(
    r"\b(?:Py_DECREF|Py_XDECREF|Py_CLEAR|PyObject_GC_Del|PyObject_Free)\s*\(\s*\(?\s*(\w+)"
)

# `.tp_dealloc = X` / `{Py_tp_dealloc, X}`
_TP_DEALLOC_DESIGNATED = re.compile(
    r"\.tp_dealloc\s*=\s*(?:\(\s*\w[\w\s\*]*\)\s*)?(?:&\s*)?(\w+)"
)
_TP_DEALLOC_SPEC = re.compile(r"\{\s*Py_tp_dealloc\s*,\s*(?:&\s*)?(\w+)\s*\}")


def _split_top_level_functions(lines: list[str]) -> list[dict]:
    """Split a stripped C file into column-0 function definitions.

    Deliberately regex-based and local: this scanner keeps exact line numbers
    by never re-deriving them from a second parse.
    """
    funcs: list[dict] = []
    header = re.compile(r"^(\w+)\s*\(")
    for i, line in enumerate(lines):
        m = header.match(line)
        if not m:
            continue
        # Must actually open a body: find `{` at column 0 within a few lines.
        body_start = None
        for j in range(i, min(i + 12, len(lines))):
            if lines[j].startswith("{"):
                body_start = j
                break
            if lines[j].rstrip().endswith(";"):
                break
        if body_start is None:
            continue
        end = len(lines) - 1
        for j in range(body_start + 1, len(lines)):
            if lines[j].startswith("}"):
                end = j
                break
        funcs.append(
            {
                "name": m.group(1),
                "start": i,
                "end": end,
                "body": "\n".join(lines[body_start:end]),
                "body_lines": lines[body_start:end],
                "body_offset": body_start,
            }
        )
    return funcs


def find_pretrack_free_constructors(funcs: list[dict]) -> dict[str, dict]:
    """Constructors with a free path *before* the object is GC-tracked.

    Returns ``{constructor_name: {"type": <PyTypeObject symbol>, "line": N}}``.
    """
    result: dict[str, dict] = {}
    for func in funcs:
        body_lines = func["body_lines"]
        assign = None
        alloc_idx = None
        type_symbol = ""
        for idx, line in enumerate(body_lines):
            m = _GC_NEW_ASSIGN_RE.search(line)
            if m:
                assign = m.group(1)
                alloc_idx = idx
                t = _GC_NEW_RE.search(line)
                if t:
                    type_symbol = t.group(1)
                break
        if assign is None or alloc_idx is None:
            continue
        track_idx = None
        for idx in range(alloc_idx + 1, len(body_lines)):
            m = _GC_TRACK_RE.search(body_lines[idx])
            if m and m.group(1) == assign:
                track_idx = idx
                break
        if track_idx is None:
            continue
        for idx in range(alloc_idx + 1, track_idx):
            m = _FREE_CALL_RE.search(body_lines[idx])
            if m and m.group(1) == assign:
                result[func["name"]] = {
                    "type": type_symbol,
                    "line": func["body_offset"] + idx + 1,
                    "variable": assign,
                }
                break
    return result


def _dealloc_for_type(source: str, type_symbol: str) -> str:
    """Return the ``tp_dealloc`` slot function of ``type_symbol``, if findable."""
    if not type_symbol:
        return ""
    m = re.search(
        rf"^\s*(?:static\s+)?PyTypeObject\s+{re.escape(type_symbol)}\s*=(.*?)^\}}",
        source,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return ""
    block = m.group(1)
    d = _TP_DEALLOC_DESIGNATED.search(block) or _TP_DEALLOC_SPEC.search(block)
    return d.group(1) if d else ""


def scan_gc_untrack(source: str, lines: list[str], rel: str) -> list[dict]:
    """Emit ``gc-untrack-macro-form`` findings for one file."""
    funcs = _split_top_level_functions(lines)
    constructors = find_pretrack_free_constructors(funcs)
    if not constructors:
        return []
    by_name = {f["name"]: f for f in funcs}

    findings: list[dict] = []
    for ctor_name, info in constructors.items():
        dealloc = _dealloc_for_type(source, info["type"])
        link = "tp_dealloc slot"
        if not dealloc or dealloc not in by_name:
            # Fall back to the shared-prefix convention (foo_new / foo_dealloc).
            stem = re.sub(r"_(?:new|alloc|create)$", "", ctor_name)
            candidate = f"{stem}_dealloc"
            if candidate in by_name:
                dealloc = candidate
                link = "name prefix"
            else:
                continue
        func = by_name[dealloc]
        for idx, line in enumerate(func["body_lines"]):
            if not _UNTRACK_MACRO_RE.search(line):
                continue
            findings.append(
                {
                    "type": "gc-untrack-macro-form",
                    "rule": "gc-untrack-macro-form",
                    "file": rel,
                    "line": func["body_offset"] + idx + 1,
                    "function": dealloc,
                    "severity": "FIX",
                    "confidence": "high",
                    "api": "_PyObject_GC_UNTRACK",
                    "replacement": "PyObject_GC_UnTrack",
                    "detail": (
                        f"_PyObject_GC_UNTRACK in tp_dealloc '{dealloc}', but the "
                        f"constructor '{ctor_name}' frees the object at line "
                        f"{info['line']} before _PyObject_GC_TRACK runs "
                        f"(linked via {link}). On that error path dealloc gets an "
                        "untracked object and the macro's "
                        "_PyObject_ASSERT_FROM(op, _PyObject_GC_IS_TRACKED(op)) "
                        "aborts -- a recoverable allocation failure becomes a "
                        "SIGABRT. Use the untracked-tolerant PyObject_GC_UnTrack "
                        "here. (It is tolerant of the untracked *state*, not of a "
                        "NULL pointer: _PyObject_GC_IS_TRACKED still dereferences "
                        "unconditionally.)"
                    ),
                }
            )
            break
    return findings


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------


def _severity(api: dict) -> str:
    return "FIX" if api.get("removed_in") else "CONSIDER"


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan for deprecated CPython C API usage under ``target``."""
    project_root, scan_root = resolve_roots(target)
    apis = load_deprecated_apis()
    patterns = [
        (api, re.compile(rf"\b{re.escape(api['name'])}\b")) for api in apis
    ]

    findings: list[dict] = []
    files_analyzed = 0
    by_api: dict[str, int] = defaultdict(int)
    by_tier: dict[str, int] = defaultdict(int)
    by_severity: dict[str, int] = defaultdict(int)

    for filepath in discover_c_files(scan_root, max_files=max_files):
        try:
            raw = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files_analyzed += 1
        rel = relpath(filepath, project_root)
        clean = strip_comments_and_strings(raw)
        lines = clean.split("\n")
        raw_lines = raw.split("\n")

        for api, pattern in patterns:
            name = api["name"]
            if name not in clean:
                continue
            def_ranges = find_definition_ranges(lines, name)
            for i, line in enumerate(lines):
                if not pattern.search(line):
                    continue
                if _in_ranges(i, def_ranges):
                    continue
                if _is_declaration_line(line, name):
                    continue
                severity = _severity(api)
                findings.append(
                    {
                        "type": "deprecated-api",
                        "rule": "deprecated-api",
                        "file": rel,
                        "line": i + 1,
                        "api": name,
                        "tier": api.get("tier", "soft"),
                        "deprecated_in": api.get("deprecated_in", ""),
                        "removed_in": api.get("removed_in", ""),
                        "replacement": api.get("replacement", ""),
                        "severity": severity,
                        "confidence": "high",
                        "code": raw_lines[i].strip()[:160] if i < len(raw_lines) else "",
                        "detail": _describe(api),
                    }
                )
                by_api[name] += 1
                by_tier[api.get("tier", "soft")] += 1
                by_severity[severity] += 1

        for finding in scan_gc_untrack(raw, lines, rel):
            findings.append(finding)
            by_api["_PyObject_GC_UNTRACK"] += 1
            by_tier["safety-rule"] += 1
            by_severity["FIX"] += 1

    findings.sort(key=lambda f: (f["file"], f["line"]))

    return build_report(
        project_root=project_root,
        scan_root=scan_root,
        files_analyzed=files_analyzed,
        functions_analyzed=0,
        findings=findings,
        summary={
            "total_findings": len(findings),
            "by_api": dict(sorted(by_api.items(), key=lambda x: -x[1])),
            "by_tier": dict(by_tier),
            "by_severity": dict(by_severity),
            "apis_in_vocabulary": len(apis),
            "apis_with_hits": len(by_api),
        },
        vocabulary_source="data/deprecated_c_apis.json",
    )


def _describe(api: dict) -> str:
    name = api["name"]
    bits = [f"{name} is deprecated"]
    if api.get("deprecated_in"):
        bits.append(f"since {api['deprecated_in']}")
    if api.get("removed_in"):
        bits.append(f"and is scheduled for removal in {api['removed_in']}")
    text = " ".join(bits) + "."
    if api.get("replacement"):
        text += f" Use {api['replacement']} instead."
    if api.get("tier") == "hard-internal":
        text += (
            " Marked _Py_DEPRECATED_EXTERNALLY, which expands to nothing under"
            " Py_BUILD_CORE -- CPython's own build emits no warning for this"
            " call, so it will not show up in a compiler log."
        )
    if api.get("notes"):
        text += f" ({api['notes']})"
    return text


def main() -> None:
    target, max_files = parse_common_args(sys.argv[1:])
    result = analyze(target, max_files=max_files)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

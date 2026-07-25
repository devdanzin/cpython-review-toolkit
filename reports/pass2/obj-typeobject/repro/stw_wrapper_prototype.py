"""Prototype of the proposed scan_stw_safety.py fix: resolve file-local
one-line wrappers around _PyEval_Stop/StartTheWorld and treat them as region
delimiters.

Usage:  python stw_wrapper_prototype.py <path-under-cpython> [--max-files N]

Prints a before/after comparison: STW-region-opening functions and findings,
with the stock scanner and with wrapper resolution enabled.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

SCRIPTS = Path(
    "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts"
)
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("scan_stw", SCRIPTS / "scan_stw_safety.py")
assert spec and spec.loader
stw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stw)

from scan_common import discover_c_files, relpath, resolve_roots  # noqa: E402
from tree_sitter_utils import extract_functions, parse_bytes  # noqa: E402

_ASSERT_RE = re.compile(r"\bassert\s*\([^;]*\)\s*;")
_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
# control-flow keywords and thread/interp-state getters are not "work"
_ALLOW = {
    "sizeof",
    "if",
    "while",
    "for",
    "switch",
    "return",
    "_PyInterpreterState_GET",
    "_PyRuntimeState_GET",
    "_PyThreadState_GET",
}
_STOP_PRIMS = {"_PyEval_StopTheWorld", "_PyEval_StopTheWorldAll"}
_START_PRIMS = {"_PyEval_StartTheWorld", "_PyEval_StartTheWorldAll"}


def wrapper_kind(body: str) -> str | None:
    """'stop' / 'start' if `body` is a trivial wrapper around the primitive."""
    stripped = _ASSERT_RE.sub("", body)
    names = set(_CALL_RE.findall(stripped)) - _ALLOW
    if names and names <= _STOP_PRIMS:
        return "stop"
    if names and names <= _START_PRIMS:
        return "start"
    return None


def scan(target: str, *, resolve_wrappers: bool, max_files: int = 0) -> dict:
    project_root, scan_root = resolve_roots(target)
    safe_apis = stw._get_safe_apis()
    unsafe_apis = stw._get_unsafe_apis()
    unsafe_prop = stw._get_unsafe_apis_for_propagation()
    unsafe_cats = stw._get_unsafe_categories()

    findings: list[dict] = []
    stw_functions: list[dict] = []
    wrappers_found: list[dict] = []
    files = 0

    orig_stop, orig_start = stw._STOP_RE, stw._START_RE
    orig_control = set(stw._STW_CONTROL)

    for filepath in discover_c_files(scan_root, max_files=max_files):
        try:
            source_bytes = filepath.read_bytes()
            tree = parse_bytes(source_bytes)
        except Exception:
            continue
        functions = extract_functions(tree, source_bytes)
        if not functions:
            continue
        text = source_bytes.decode("utf-8", errors="replace")
        if "_PyEval_StopTheWorld" not in text:
            continue
        files += 1
        rel = relpath(filepath, project_root)

        stops, starts = set(), set()
        if resolve_wrappers:
            for f in functions:
                kind = wrapper_kind(f["body"])
                if kind == "stop":
                    stops.add(f["name"])
                elif kind == "start":
                    starts.add(f["name"])
            for n in sorted(stops | starts):
                wrappers_found.append(
                    {"file": rel, "wrapper": n, "kind": "stop" if n in stops else "start"}
                )

        stop_alt = "|".join(sorted(_STOP_PRIMS | stops))
        start_alt = "|".join(sorted(_START_PRIMS | starts))
        stw._STOP_RE = re.compile(r"\b(?:" + stop_alt + r")\s*\(")
        stw._START_RE = re.compile(r"\b(?:" + start_alt + r")\s*\(")
        stw._STW_CONTROL = orig_control | stops | starts

        graph = stw._build_call_graph(functions, source_bytes)
        classifications = stw._propagate_stw_safety(graph, safe_apis, unsafe_prop)

        for func in functions:
            for fi in stw._check_stw_regions(
                func, source_bytes, tree, classifications, safe_apis, unsafe_apis, unsafe_cats
            ):
                fi["file"] = rel
                findings.append(fi)
            if stw._STOP_RE.search(func["body"]):
                stw_functions.append(
                    {
                        "file": rel,
                        "function": func["name"],
                        "line": func["start_line"],
                        "classification": classifications.get(func["name"], "unknown"),
                    }
                )

    stw._STOP_RE, stw._START_RE = orig_stop, orig_start
    stw._STW_CONTROL = orig_control

    by_type: dict[str, int] = defaultdict(int)
    for f in findings:
        by_type[f["type"]] += 1
    return {
        "files_with_stw": files,
        "stw_functions": stw_functions,
        "wrappers_found": wrappers_found,
        "findings": findings,
        "by_type": dict(by_type),
    }


def main() -> None:
    target = sys.argv[1]
    max_files = 0
    if "--max-files" in sys.argv:
        max_files = int(sys.argv[sys.argv.index("--max-files") + 1])
    before = scan(target, resolve_wrappers=False, max_files=max_files)
    after = scan(target, resolve_wrappers=True, max_files=max_files)
    out = {
        "target": target,
        "before": {
            "stw_function_count": len(before["stw_functions"]),
            "finding_count": len(before["findings"]),
            "by_type": before["by_type"],
            "stw_functions": before["stw_functions"],
        },
        "after": {
            "stw_function_count": len(after["stw_functions"]),
            "finding_count": len(after["findings"]),
            "by_type": after["by_type"],
            "wrappers_found": after["wrappers_found"],
            "stw_functions": after["stw_functions"],
            "findings": after["findings"],
        },
    }
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

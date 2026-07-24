#!/usr/bin/env python3
"""Scan CPython C source for StopTheWorld-safety violations in its own code.

During ``_PyEval_StopTheWorld()`` (the free-threaded build's global pause) every
*other* thread is suspended at a safe point. The stopping thread then has
exclusive access to object graphs — but any operation that could invoke Python
code, set an exception via the format machinery, or take a lock a stopped thread
already holds is unsafe: it can deadlock the world or corrupt interpreter state.
CPython encodes this contract in its own GC: ``Python/gc_free_threading.c:2223``
calls ``_PyEval_StartTheWorld`` *before* ``PyErr_NoMemory``. This scanner audits
CPython's own ``Python/`` / ``Objects/`` / ``Modules/`` code against that rule.

Approach (mirrors the ft-review-toolkit reference this is ported from):

1. Build an *intra-file* call graph with ``extract_functions`` +
   ``find_calls_in_scope``.
2. Propagate STW-safety through it: a function is unsafe if any transitive
   callee (in the same file) is Python-invoking, keyed off
   ``data/stw_safe_apis.json``.
3. Find each ``_PyEval_StopTheWorld(...)..._PyEval_StartTheWorld(...)`` region and
   flag every call inside it that resolves to unsafe (directly, or transitively
   via a local helper) or that cannot be classified.

Honest limitation: the call graph is intra-file only. A helper defined in
another translation unit is treated as an unclassified (``unknown``) call, not
followed. The agent triages those.

Usage:
    python scan_stw_safety.py [path] [--max-files N]
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
    is_suppressed_by_comment,
    parse_common_args,
    relpath,
    resolve_roots,
)
from tree_sitter_utils import (
    extract_functions,
    find_calls_in_scope,
    get_node_text,
    parse_bytes,
)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_stw_data: dict | None = None

# STW control functions are never violations themselves (the pairing/lock
# discipline checker owns Stop/Start balance). They bracket the region.
_STW_CONTROL = frozenset(
    {
        "_PyEval_StopTheWorld",
        "_PyEval_StopTheWorldAll",
        "_PyEval_StartTheWorld",
        "_PyEval_StartTheWorldAll",
    }
)

_STOP_RE = re.compile(r"_PyEval_StopTheWorld(?:All)?\s*\(")
_START_RE = re.compile(r"_PyEval_StartTheWorld(?:All)?\s*\(")

# Standard C library functions are STW-safe (no Python involvement).
_C_STDLIB = frozenset(
    {
        "memcpy",
        "memmove",
        "memset",
        "memcmp",
        "strlen",
        "strcmp",
        "strncmp",
        "strcpy",
        "strncpy",
        "printf",
        "fprintf",
        "sprintf",
        "snprintf",
        "malloc",
        "calloc",
        "realloc",
        "free",
        "assert",
        "sizeof",
        "offsetof",
        "abs",
        "labs",
    }
)


# ---------------------------------------------------------------------------
# Data-file loading + API classification
# ---------------------------------------------------------------------------


def _load_stw_apis() -> dict:
    """Load STW safety classifications from ``data/stw_safe_apis.json``."""
    global _stw_data
    if _stw_data is not None:
        return _stw_data
    try:
        with open(_DATA_DIR / "stw_safe_apis.json", encoding="utf-8") as f:
            _stw_data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Warning: failed to load stw_safe_apis.json: {e}", file=sys.stderr)
        _stw_data = {}
    return _stw_data


def _extract_apis_from_value(value: object) -> set[str]:
    """Extract API names from a data-file value (list, or dict with sub-lists)."""
    apis: set[str] = set()
    if isinstance(value, list):
        apis.update(value)
    elif isinstance(value, dict):
        for sub_val in value.values():
            if isinstance(sub_val, list):
                apis.update(sub_val)
        # Conditional entries store the API name as the key (value is a
        # human-readable condition string).
        for sub_key, sub_val in value.items():
            if isinstance(sub_val, str) and not sub_key.startswith("_"):
                apis.add(sub_key)
    return apis


def _get_safe_apis() -> set[str]:
    """All APIs classified safe during STW.

    On 3.14+ free-threading builds allocation APIs are safe (GC runs only on the
    eval breaker, not during allocation). Conditionally-safe exception APIs are
    treated as safe by the scanner — the "no exception set" precondition cannot
    be verified statically, so the agent triages them.
    """
    data = _load_stw_apis()
    unsafe_section = data.get("unsafe_during_stw", {})
    safe: set[str] = set()
    for category in data.get("safe_during_stw", {}).values():
        if isinstance(category, list):
            safe.update(category)
    # ``safe_allocation_on_314`` lives *inside* ``unsafe_during_stw`` in the data
    # file (it documents the 3.14+ carve-out from the historically-unsafe alloc
    # APIs), so it must be read from there — a top-level lookup silently misses
    # it and the whole 3.14+ "allocation is safe" revision never takes effect.
    alloc_314 = unsafe_section.get("safe_allocation_on_314", {})
    safe_alloc = alloc_314.get("safe_if_builtin_types_only", [])
    if isinstance(safe_alloc, list):
        safe.update(safe_alloc)
    exc = unsafe_section.get("exception_setting", {})
    cond_safe = exc.get("conditionally_safe_during_stw", {})
    if isinstance(cond_safe, dict):
        for api_name in cond_safe:
            if not api_name.startswith("_"):
                safe.add(api_name)
    return safe


def _get_unsafe_apis() -> set[str]:
    """All APIs classified unsafe during STW (safe wins on overlap)."""
    data = _load_stw_apis()
    unsafe: set[str] = set()
    for category in data.get("unsafe_during_stw", {}).values():
        unsafe.update(_extract_apis_from_value(category))
    unsafe -= _get_safe_apis()
    return unsafe


def _get_unsafe_apis_for_propagation() -> set[str]:
    """Unsafe APIs used for call-graph propagation (excludes STW control).

    Excluding the ``stw_start`` category keeps a function that legitimately opens
    a StopTheWorld region from being mislabelled "invokes Python".
    """
    data = _load_stw_apis()
    unsafe: set[str] = set()
    for cat_name, apis in data.get("unsafe_during_stw", {}).items():
        if cat_name != "stw_start":
            unsafe.update(_extract_apis_from_value(apis))
    return unsafe


def _get_unsafe_categories() -> dict[str, set[str]]:
    """Unsafe APIs grouped by their data-file category (for reason reporting)."""
    data = _load_stw_apis()
    categories: dict[str, set[str]] = {}
    for cat_name, apis in data.get("unsafe_during_stw", {}).items():
        extracted = _extract_apis_from_value(apis)
        if extracted:
            categories[cat_name] = extracted
    return categories


def _classify_call(func_name: str, safe_apis: set[str], unsafe_apis: set[str]) -> str:
    """Classify a single call as ``safe`` / ``unsafe`` / ``unknown``."""
    if func_name in safe_apis:
        return "safe"
    if func_name in unsafe_apis:
        return "unsafe"
    if func_name in _C_STDLIB:
        return "safe"
    return "unknown"


def _get_unsafe_reason(func_name: str, unsafe_categories: dict[str, set[str]]) -> str:
    """Return the data-file category explaining why an API is unsafe."""
    for cat_name, apis in unsafe_categories.items():
        if func_name in apis:
            return cat_name
    return "unknown"


# ---------------------------------------------------------------------------
# STW region detection + call-graph propagation
# ---------------------------------------------------------------------------


def _find_stw_regions(body_text: str) -> list[tuple[int, int]]:
    """Byte-offset ranges between StopTheWorld and the next StartTheWorld.

    Offsets are relative to ``body_text``. Each Stop is paired with the first
    Start that follows it.
    """
    regions: list[tuple[int, int]] = []
    stops = list(_STOP_RE.finditer(body_text))
    starts = list(_START_RE.finditer(body_text))
    for s in stops:
        for st in starts:
            if st.start() > s.end():
                regions.append((s.end(), st.start()))
                break
    return regions


def _is_in_region(offset: int, regions: list[tuple[int, int]]) -> bool:
    """True if ``offset`` falls within any (start, end) region."""
    return any(start <= offset < end for start, end in regions)


def _build_call_graph(
    functions: list[dict], source_bytes: bytes
) -> dict[str, list[str]]:
    """Map each function name to the list of functions it calls (intra-file)."""
    graph: dict[str, list[str]] = {}
    for func in functions:
        calls = find_calls_in_scope(func["body_node"], source_bytes)
        graph[func["name"]] = [c["function_name"] for c in calls]
    return graph


def _propagate_stw_safety(
    graph: dict[str, list[str]],
    safe_apis: set[str],
    unsafe_apis: set[str],
) -> dict[str, str]:
    """Propagate STW-safety through the call graph.

    A function is unsafe if any transitive callee is unsafe, unknown if a callee
    is unknown (and none is unsafe), otherwise safe. Recursive edges are treated
    as safe to break the cycle.
    """
    classifications: dict[str, str] = {}

    def classify(func_name: str, visited: set[str]) -> str:
        if func_name in classifications:
            return classifications[func_name]
        if func_name in visited:
            return "safe"  # recursion — break the cycle
        visited.add(func_name)

        if func_name not in graph:
            result = _classify_call(func_name, safe_apis, unsafe_apis)
            classifications[func_name] = result
            return result

        result = "safe"
        for callee in graph[func_name]:
            callee_class = classify(callee, visited)
            if callee_class == "unsafe":
                result = "unsafe"
                break
            if callee_class == "unknown" and result == "safe":
                result = "unknown"

        classifications[func_name] = result
        return result

    for func_name in graph:
        classify(func_name, set())
    return classifications


def _check_stw_regions(
    func: dict,
    source_bytes: bytes,
    tree,
    classifications: dict[str, str],
    safe_apis: set[str],
    unsafe_apis: set[str],
    unsafe_categories: dict[str, set[str]],
) -> list[dict]:
    """Flag unsafe / unclassified calls inside this function's STW regions."""
    findings: list[dict] = []
    body_node = func["body_node"]
    body_text = get_node_text(body_node, source_bytes)
    regions = _find_stw_regions(body_text)
    if not regions:
        return findings

    for call in find_calls_in_scope(body_node, source_bytes):
        call_name = call["function_name"]
        if call_name in _STW_CONTROL:
            continue
        # Offset of the call relative to the body node (same frame as regions).
        call_offset = call["start_byte"] - body_node.start_byte
        if call_offset < 0 or not _is_in_region(call_offset, regions):
            continue
        if is_suppressed_by_comment(source_bytes, tree, call["start_line"]):
            continue

        # Prefer the propagated classification (covers local helpers); fall back
        # to direct API classification for calls never reached by propagation.
        call_class = classifications.get(call_name)
        if call_class is None:
            call_class = _classify_call(call_name, safe_apis, unsafe_apis)

        if call_class == "safe":
            continue

        if call_class == "unsafe":
            reason = _get_unsafe_reason(call_name, unsafe_categories)
            if not reason or reason == "unknown":
                reason = "transitively_invokes_python"

            finding_type = "stw_unsafe_call"
            if reason == "exception_setting":
                finding_type = "stw_exception_during_stw"
            elif reason == "may_trigger_gc_or_alloc":
                finding_type = "stw_allocation_during_stw"

            findings.append(
                {
                    "type": finding_type,
                    "function": func["name"],
                    "line": call["start_line"],
                    "confidence": "high",
                    "detail": (
                        f"Unsafe call {call_name}() inside a _PyEval_StopTheWorld "
                        f"region in '{func['name']}'. During STW every other thread "
                        f"is suspended; this call may invoke Python code, set an "
                        f"exception via the format machinery, or take a lock a "
                        f"stopped thread holds — which can deadlock the world or "
                        f"corrupt interpreter state on the free-threaded build. "
                        f"Category: {reason}. Call _PyEval_StartTheWorld before it "
                        f"(cf. Python/gc_free_threading.c:2223). Intra-file analysis: "
                        f"transitive callees in other files are not followed."
                    ),
                    "api_call": call_name,
                    "unsafe_reason": reason,
                }
            )

        elif call_class == "unknown":
            findings.append(
                {
                    "type": "stw_unknown_call",
                    "function": func["name"],
                    "line": call["start_line"],
                    "confidence": "medium",
                    "detail": (
                        f"Unclassified call {call_name}() inside a "
                        f"_PyEval_StopTheWorld region in '{func['name']}'. Cannot "
                        f"statically determine whether it may invoke Python code — "
                        f"it is not in the STW API vocabulary and (if local) its "
                        f"body reaches an unclassified callee. Manual review needed. "
                        f"Intra-file analysis only."
                    ),
                    "api_call": call_name,
                }
            )

    return findings


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Scan CPython C source for StopTheWorld-safety violations."""
    project_root, scan_root = resolve_roots(target)

    safe_apis = _get_safe_apis()
    unsafe_apis = _get_unsafe_apis()
    unsafe_apis_for_prop = _get_unsafe_apis_for_propagation()
    unsafe_categories = _get_unsafe_categories()

    findings: list[dict] = []
    files_analyzed = 0
    functions_analyzed = 0
    skipped: list[dict] = []
    all_classifications: dict[str, dict[str, str]] = {}
    stw_functions: list[dict] = []

    for filepath in discover_c_files(scan_root, max_files=max_files):
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

        functions = extract_functions(tree, source_bytes)
        if not functions:
            continue

        files_analyzed += 1
        functions_analyzed += len(functions)

        # Only files that actually use StopTheWorld can have a violation.
        source_text = source_bytes.decode("utf-8", errors="replace")
        if "_PyEval_StopTheWorld" not in source_text:
            continue

        rel = relpath(filepath, project_root)
        graph = _build_call_graph(functions, source_bytes)
        classifications = _propagate_stw_safety(graph, safe_apis, unsafe_apis_for_prop)
        all_classifications[rel] = classifications

        for func in functions:
            for f in _check_stw_regions(
                func,
                source_bytes,
                tree,
                classifications,
                safe_apis,
                unsafe_apis,
                unsafe_categories,
            ):
                f["file"] = rel
                findings.append(f)

            if "_PyEval_StopTheWorld" in func["body"]:
                stw_functions.append(
                    {
                        "file": rel,
                        "function": func["name"],
                        "line": func["start_line"],
                        "classification": classifications.get(func["name"], "unknown"),
                    }
                )

    by_type: dict[str, int] = defaultdict(int)
    by_confidence: dict[str, int] = defaultdict(int)
    for f in findings:
        by_type[f["type"]] += 1
        by_confidence[f["confidence"]] += 1

    return build_report(
        project_root=project_root,
        scan_root=scan_root,
        files_analyzed=files_analyzed,
        functions_analyzed=functions_analyzed,
        findings=findings,
        summary={
            "total_findings": len(findings),
            "by_type": dict(by_type),
            "by_confidence": dict(by_confidence),
            "stw_function_count": len(stw_functions),
        },
        stw_functions=stw_functions,
        function_classifications=all_classifications,
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

#!/usr/bin/env python3
"""Analyze the #include dependency graph across CPython C source files.

Every include directive is **resolved to a real path on disk before it is
used**. That single property is what makes the tier and cycle outputs mean
anything on CPython: the tree includes its internal headers by bare name
(``#include "pycore_object.h"``, because ``Include/internal`` is on the build
include path), so classifying or graph-keying on the directive *text* buckets
148 ``pycore_*.h`` headers as "public" and leaves the include graph with edges
that match no node (only 5 of 1110 edge targets), making ``cycles: []`` a
tautology rather than a result.

Outputs a JSON structure with:

``include_graph``
    file -> [{header, kind, resolved, tier}] for every directive.
``reverse_graph``
    resolved header -> [files including it]. The preflight question is "who
    depends on me?", which the forward graph cannot answer.
``fan_in``
    most-included headers, ranked. Counted **tree-wide** even when the scan is
    narrowed to a subdirectory (``fan_in_within_scope`` carries the scoped
    number alongside), because a scope-local count silently understates by
    3-4x — ``pycore_tuple.h`` is 20 within ``Objects/`` and 67 tree-wide.
``symbol_fan_in``
    files referencing each public header's exported symbols. This is the
    number to trust for blast radius: because ``Include/Python.h`` is a
    mega-include, *every* public header has an include fan-in of 1, while
    ``PyTuple_*`` is referenced by hundreds of files.
``fan_out``
    files with the most includes, ranked.
``cycles``
    circular include chains over the resolved graph.
``api_tiers``
    resolved headers bucketed public / cpython / internal / generated /
    vendored / other-local / unresolved / system.

Usage:
    python analyze_includes.py [path] [--max-files N] [--no-symbol-fan-in]

    path: directory, file, or omitted for current directory
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Generator


# ---------------------------------------------------------------------------
# CPython root detection and file discovery
# ---------------------------------------------------------------------------


def find_cpython_root(start: Path) -> Path | None:
    """Walk up from *start* looking for CPython root markers.

    CPython root is identified by the presence of both
    Include/Python.h and Objects/object.c.
    """
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


_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".tox",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        ".eggs",
    }
)


def discover_c_files(
    root: Path,
    *,
    max_files: int = 0,
) -> Generator[Path, None, None]:
    """Yield .c and .h files under *root*, excluding non-source dirs."""
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
# Include extraction
# ---------------------------------------------------------------------------

_INCLUDE_RE = re.compile(r'^\s*#\s*include\s+([<"])(.+?)[>"]', re.MULTILINE)


def extract_includes(source: str) -> list[dict[str, str]]:
    """Extract all #include directives from C source text.

    Returns a list of dicts with keys:
      - header: the included file path
      - kind: "system" (<...>) or "local" ("...")
    """
    results = []
    for m in _INCLUDE_RE.finditer(source):
        delim = m.group(1)
        header = m.group(2)
        kind = "system" if delim == "<" else "local"
        results.append({"header": header, "kind": kind})
    return results


# ---------------------------------------------------------------------------
# Directive resolution
# ---------------------------------------------------------------------------

# The build's include path, in the order the preprocessor would search it.
_INCLUDE_SEARCH_DIRS: tuple[tuple[str, ...], ...] = (
    ("Include",),
    ("Include", "internal"),
    ("Include", "cpython"),
    ("Include", "internal", "mimalloc"),
    (),
)


def resolve_include(header: str, includer: Path, project_root: Path) -> Path | None:
    """Resolve an include directive to a file on disk.

    Search order mirrors the preprocessor: the including file's own directory
    first, then the build include path (``Include/``, ``Include/internal/``,
    ``Include/cpython/``), then the project root. Returns None when the
    directive names no file in the tree (platform-conditional headers such as
    ``windows.h``, generated headers that do not exist in a clean checkout).
    """
    if header.startswith("/"):
        return None
    candidates = [includer.parent / header]
    candidates += [project_root.joinpath(*d, header) for d in _INCLUDE_SEARCH_DIRS]
    for cand in candidates:
        try:
            resolved = cand.resolve()
        except OSError:  # pragma: no cover - defensive
            continue
        if resolved.is_file():
            try:
                resolved.relative_to(project_root.resolve())
            except ValueError:
                continue
            return resolved
    return None


# ---------------------------------------------------------------------------
# API tier classification
# ---------------------------------------------------------------------------

_VENDORED_PREFIXES: tuple[str, ...] = (
    "Modules/_hacl/",
    "Modules/expat/",
    "Modules/_decimal/libmpdec/",
    "Modules/zlib/",
    "Objects/mimalloc/",
    "Include/internal/mimalloc/",
    "Modules/_ctypes/libffi",
)


def classify_api_tier(header: str) -> str:
    """Classify a header into CPython's API tiers.

    Accepts either a **resolved repo-relative path** (the useful case) or a raw
    directive string (kept working for callers that only have the text). The
    ``pycore_`` name check is what saves the bare-name spelling CPython
    actually uses: ``#include "pycore_object.h"`` is an *internal* header even
    though nothing in the text says so.
    """
    path = header.replace("\\", "/")
    name = path.rsplit("/", 1)[-1]

    for prefix in _VENDORED_PREFIXES:
        if path.startswith(prefix):
            return "vendored"
    if "/clinic/" in path or path.startswith("clinic/"):
        return "generated"

    if path.startswith(("Include/internal/", "internal/")) or "/internal/" in path:
        return "internal"
    if path.startswith(("Include/cpython/", "cpython/")) or "/cpython/" in path:
        return "cpython"
    if name.startswith("pycore_"):
        return "internal"
    if path.startswith("Include/"):
        return "public"
    if "/" not in path:
        # A bare directive we could not resolve; Include/ is the default home
        # of the public API, which is what the historical behaviour assumed.
        return "public"
    return "other-local"


# ---------------------------------------------------------------------------
# Graph analysis
# ---------------------------------------------------------------------------


def detect_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Detect cycles in a directed graph using an iterative DFS.

    Returns a list of cycles, each as a list of nodes forming the cycle.
    Iterative rather than recursive because the resolved include graph has
    header->header edges and real chain depth; a recursive DFS blows the Python
    stack on a full CPython tree.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = defaultdict(int)
    cycles: list[list[str]] = []
    seen_cycles: set[tuple[str, ...]] = set()

    for root in sorted(graph):
        if color[root] != WHITE:
            continue
        path: list[str] = []
        # Stack entries: (node, iterator over neighbours, entered?)
        stack: list[tuple[str, int]] = [(root, 0)]
        color[root] = GRAY
        path.append(root)
        while stack:
            node, idx = stack[-1]
            neighbors = graph.get(node, [])
            if idx >= len(neighbors):
                stack.pop()
                path.pop()
                color[node] = BLACK
                continue
            stack[-1] = (node, idx + 1)
            neighbor = neighbors[idx]
            if color[neighbor] == GRAY:
                start = path.index(neighbor)
                cycle = path[start:] + [neighbor]
                key = tuple(cycle)
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    cycles.append(cycle)
            elif color[neighbor] == WHITE:
                color[neighbor] = GRAY
                path.append(neighbor)
                stack.append((neighbor, 0))
    return cycles


# ---------------------------------------------------------------------------
# Symbol fan-in
# ---------------------------------------------------------------------------

_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_EXPORT_RE = re.compile(r"PyAPI_(?:FUNC|DATA)\s*\(([^)]*)\)\s*\**\s*([A-Za-z_]\w*)")
_MACRO_DEF_RE = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)", re.MULTILINE)
_TYPE_OBJ_RE = re.compile(r"\bPyTypeObject\s+\**\s*([A-Za-z_]\w*)")

# Macros that are part of every header's boilerplate, not part of its API.
_SYMBOL_NOISE_RE = re.compile(r"^(?:Py_)?[A-Z0-9_]*_H$|^Py_CPYTHON_|^Py_LIMITED_API$")


def extract_exported_symbols(source: str) -> set[str]:
    """Extract the C-API symbols a header publishes.

    Covers the three spellings that matter: ``PyAPI_FUNC``/``PyAPI_DATA``
    declarations, ``PyTypeObject`` definitions, and ``#define``d macros (a
    large part of the public surface is macros -- ``PyTuple_GET_ITEM``,
    ``Py_INCREF``).
    """
    symbols: set[str] = set()
    for _, name in _EXPORT_RE.findall(source):
        symbols.add(name)
    for name in _TYPE_OBJ_RE.findall(source):
        symbols.add(name)
    for name in _MACRO_DEF_RE.findall(source):
        if _SYMBOL_NOISE_RE.match(name):
            continue
        symbols.add(name)
    return symbols


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------


def analyze(target: str, *, max_files: int = 0, symbol_fan_in: bool = True) -> dict:
    """Analyze the include graph for the given target path.

    Returns a dict suitable for JSON serialization.
    """
    target_path = Path(target).resolve()
    project_root = find_cpython_root(target_path)
    if project_root is None:
        project_root = target_path if target_path.is_dir() else target_path.parent
    scan_root = target_path if target_path.is_dir() else target_path.parent

    def rel(p: Path) -> str:
        try:
            return str(p.relative_to(project_root))
        except ValueError:
            return str(p)

    scoped: set[str] = set()
    for filepath in discover_c_files(scan_root, max_files=max_files):
        scoped.add(rel(filepath))

    # Walk the whole tree so fan-in is never silently scope-local. The scoped
    # set above decides what lands in include_graph / fan_out.
    tree_files = list(discover_c_files(project_root))

    include_graph: dict[str, list[dict[str, str]]] = {}
    resolved_graph: dict[str, list[str]] = defaultdict(list)
    reverse_graph: dict[str, list[str]] = defaultdict(list)
    all_headers: set[str] = set()
    scoped_headers: set[str] = set()
    fan_in: dict[str, int] = defaultdict(int)
    fan_in_scoped: dict[str, int] = defaultdict(int)
    tier_of: dict[str, str] = {}
    unresolved: dict[str, int] = defaultdict(int)
    header_symbols: dict[str, set[str]] = {}
    exported: set[str] = set()
    files_analyzed = 0
    local_directives = 0
    local_unresolved = 0

    for filepath in tree_files:
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        node = rel(filepath)
        in_scope = node in scoped
        if in_scope:
            files_analyzed += 1

        if filepath.suffix == ".h" and node.startswith("Include"):
            syms = extract_exported_symbols(source)
            if syms:
                header_symbols[node] = syms
                exported |= syms

        entries: list[dict[str, str]] = []
        for inc in extract_includes(source):
            header = inc["header"]
            resolved = resolve_include(header, filepath, project_root)
            if inc["kind"] == "local":
                local_directives += 1
            if resolved is None:
                key = header
                tier = "system" if inc["kind"] == "system" else "unresolved"
                if inc["kind"] == "local":
                    local_unresolved += 1
                    unresolved[header] += 1
            else:
                key = rel(resolved)
                tier = classify_api_tier(key)

            all_headers.add(key)
            tier_of[key] = tier
            fan_in[key] += 1
            if in_scope:
                fan_in_scoped[key] += 1
                scoped_headers.add(key)
            resolved_graph[node].append(key)
            reverse_graph[key].append(node)
            entries.append(
                {
                    "header": header,
                    "kind": inc["kind"],
                    "resolved": key if resolved is not None else "",
                    "tier": tier,
                }
            )
        if in_scope:
            include_graph[node] = entries

    # Symbol fan-in: which files reference each header's exported symbols.
    symbol_files: dict[str, set[str]] = defaultdict(set)
    if symbol_fan_in and exported:
        for filepath in tree_files:
            try:
                source = filepath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            node = rel(filepath)
            for ident in set(_IDENT_RE.findall(source)) & exported:
                symbol_files[ident].add(node)

    symbol_fan_in_rows: list[dict[str, object]] = []
    for header, syms in header_symbols.items() if symbol_fan_in else ():
        files: set[str] = set()
        for sym in syms:
            files |= symbol_files.get(sym, set())
        files.discard(header)
        symbol_fan_in_rows.append(
            {
                "header": header,
                "tier": tier_of.get(header, classify_api_tier(header)),
                "exported_symbols": len(syms),
                "referencing_files": len(files),
                "include_fan_in": fan_in.get(header, 0),
            }
        )
    symbol_fan_in_rows.sort(key=lambda r: -int(r["referencing_files"]))

    fan_out: dict[str, int] = {f: len(incs) for f, incs in include_graph.items()}

    api_tiers: dict[str, list[str]] = defaultdict(list)
    for header in sorted(scoped_headers):
        api_tiers[tier_of[header]].append(header)
    tree_wide_tiers: dict[str, int] = defaultdict(int)
    for header in all_headers:
        tree_wide_tiers[tier_of[header]] += 1
    for tier in (
        "public",
        "cpython",
        "internal",
        "generated",
        "vendored",
        "other-local",
        "unresolved",
        "system",
    ):
        api_tiers.setdefault(tier, [])

    cycles = detect_cycles({k: v for k, v in resolved_graph.items()})

    ranked_fan_in = sorted(fan_in.items(), key=lambda x: -x[1])[:50]
    ranked_fan_out = sorted(fan_out.items(), key=lambda x: -x[1])[:30]

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_analyzed": files_analyzed,
        "files_in_tree": len(tree_files),
        "unique_headers": len(scoped_headers),
        "unique_headers_tree_wide": len(all_headers),
        "include_graph": include_graph,
        "reverse_graph": {k: sorted(v) for k, v in sorted(reverse_graph.items())},
        "fan_in": [
            {
                "header": h,
                "count": c,
                "within_scope": fan_in_scoped.get(h, 0),
                "tier": tier_of.get(h, "unresolved"),
            }
            for h, c in ranked_fan_in
        ],
        # Emitted in full, not truncated: this is the metric an orientation
        # agent needs per file, and a top-N list would silently omit exactly
        # the small headers a narrowed scan is about.
        "symbol_fan_in": symbol_fan_in_rows,
        "fan_out": [{"file": f, "count": c} for f, c in ranked_fan_out],
        "cycles": cycles,
        "api_tiers": dict(api_tiers),
        "unresolved_directives": [
            {"header": h, "count": c}
            for h, c in sorted(unresolved.items(), key=lambda x: -x[1])[:40]
        ],
        "summary": {
            "total_files": files_analyzed,
            "total_includes": sum(len(v) for v in include_graph.values()),
            "unique_headers": len(scoped_headers),
            "unique_headers_tree_wide": len(all_headers),
            "api_tiers_tree_wide": dict(tree_wide_tiers),
            "cycles_found": len(cycles),
            "public_headers": len(api_tiers["public"]),
            "cpython_headers": len(api_tiers["cpython"]),
            "internal_headers": len(api_tiers["internal"]),
            "generated_headers": len(api_tiers["generated"]),
            "vendored_headers": len(api_tiers["vendored"]),
            "other_local_headers": len(api_tiers["other-local"]),
            "unresolved_headers": len(api_tiers["unresolved"]),
            "system_headers": len(api_tiers["system"]),
            "local_directives": local_directives,
            "local_directives_unresolved": local_unresolved,
            "fan_in_scope": (
                "project_root (tree-wide); see fan_in[].within_scope for the "
                "scoped count"
            ),
            "api_tiers_scope": (
                "scan_root (headers included by in-scope files); see "
                "api_tiers_tree_wide for the whole checkout"
            ),
            "cycles_scope": (
                "project_root — a header->header cycle is invisible from a "
                "subdirectory scan, so cycles are always computed tree-wide"
            ),
            "symbol_fan_in_note": (
                "Include/Python.h is a mega-include, so every public header has "
                "an include fan-in of ~1. Use symbol_fan_in.referencing_files, "
                "not fan_in.count, to rank a public-tier header by blast radius."
            ),
        },
    }


def main() -> None:
    max_files = 0
    symbol_fan_in = True
    positional: list[str] = []
    unknown: list[str] = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--max-files" and i + 1 < len(argv):
            max_files = int(argv[i + 1])
            i += 2
        elif argv[i] == "--no-symbol-fan-in":
            symbol_fan_in = False
            i += 1
        elif argv[i].startswith("--"):
            unknown.append(argv[i])
            i += 1
        else:
            positional.append(argv[i])
            i += 1
    if unknown:
        print(
            f"warning: ignoring unrecognised option(s): {' '.join(unknown)}",
            file=sys.stderr,
        )
    target = positional[0] if positional else "."
    result = analyze(target, max_files=max_files, symbol_fan_in=symbol_fan_in)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

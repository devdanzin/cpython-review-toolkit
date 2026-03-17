#!/usr/bin/env python3
"""Analyze #include dependency graph across CPython C source files.

Outputs a JSON structure with:
- include_graph: directed edges from source files to included headers
- fan_in: most-included headers (ranked)
- fan_out: files with most includes (ranked)
- cycles: circular include chains
- api_tiers: classification of headers into public/cpython/internal

Usage:
    python analyze_includes.py [path]

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


_EXCLUDE_DIRS = frozenset({
    ".git", ".tox", ".venv", "venv", "__pycache__",
    "node_modules", "build", "dist", ".eggs",
})


def discover_c_files(
    root: Path, *, max_files: int = 0,
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
# Graph analysis
# ---------------------------------------------------------------------------

def classify_api_tier(header: str) -> str:
    """Classify a header into CPython's API tiers."""
    if header.startswith("internal/") or "/internal/" in header:
        return "internal"
    if header.startswith("cpython/") or "/cpython/" in header:
        return "cpython"
    # Public API headers live directly under Include/
    return "public"


def detect_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Detect cycles in a directed graph using DFS.

    Returns a list of cycles, each as a list of nodes forming the cycle.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = defaultdict(int)
    path: list[str] = []
    cycles: list[list[str]] = []

    def dfs(node: str) -> None:
        color[node] = GRAY
        path.append(node)
        for neighbor in graph.get(node, []):
            if color[neighbor] == GRAY:
                # Found a cycle — extract it.
                idx = path.index(neighbor)
                cycles.append(path[idx:] + [neighbor])
            elif color[neighbor] == WHITE:
                dfs(neighbor)
        path.pop()
        color[node] = BLACK

    for node in sorted(graph):
        if color[node] == WHITE:
            dfs(node)
    return cycles


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(target: str, *, max_files: int = 0) -> dict:
    """Analyze include graph for the given target path.

    Returns a dict suitable for JSON serialization.
    """
    target_path = Path(target).resolve()
    project_root = find_cpython_root(target_path)
    if project_root is None:
        project_root = target_path if target_path.is_dir() else target_path.parent
    scan_root = target_path if target_path.is_dir() else target_path.parent

    # Build include graph.
    include_graph: dict[str, list[dict[str, str]]] = {}
    # Simplified graph for cycle detection (file -> [included files]).
    simple_graph: dict[str, list[str]] = defaultdict(list)
    all_headers: set[str] = set()
    fan_in: dict[str, int] = defaultdict(int)
    files_analyzed = 0

    for filepath in discover_c_files(scan_root, max_files=max_files):
        files_analyzed += 1
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        rel = str(filepath.relative_to(project_root))
        includes = extract_includes(source)
        include_graph[rel] = includes
        for inc in includes:
            header = inc["header"]
            all_headers.add(header)
            fan_in[header] += 1
            simple_graph[rel].append(header)

    # Compute fan-out.
    fan_out: dict[str, int] = {
        f: len(incs) for f, incs in include_graph.items()
    }

    # Classify headers by API tier.
    api_tiers: dict[str, list[str]] = {
        "public": [], "cpython": [], "internal": [], "system": [],
    }
    for header in sorted(all_headers):
        # System headers are <...> includes not in Include/.
        is_local = any(
            inc["kind"] == "local" and inc["header"] == header
            for incs in include_graph.values()
            for inc in incs
        )
        if not is_local:
            api_tiers["system"].append(header)
        else:
            tier = classify_api_tier(header)
            api_tiers[tier].append(header)

    # Detect cycles.
    cycles = detect_cycles(dict(simple_graph))

    # Build ranked fan-in / fan-out.
    ranked_fan_in = sorted(fan_in.items(), key=lambda x: -x[1])[:30]
    ranked_fan_out = sorted(fan_out.items(), key=lambda x: -x[1])[:30]

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_analyzed": files_analyzed,
        "unique_headers": len(all_headers),
        "include_graph": include_graph,
        "fan_in": [{"header": h, "count": c} for h, c in ranked_fan_in],
        "fan_out": [{"file": f, "count": c} for f, c in ranked_fan_out],
        "cycles": cycles,
        "api_tiers": api_tiers,
        "summary": {
            "total_files": files_analyzed,
            "total_includes": sum(len(v) for v in include_graph.values()),
            "unique_headers": len(all_headers),
            "cycles_found": len(cycles),
            "public_headers": len(api_tiers["public"]),
            "cpython_headers": len(api_tiers["cpython"]),
            "internal_headers": len(api_tiers["internal"]),
            "system_headers": len(api_tiers["system"]),
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

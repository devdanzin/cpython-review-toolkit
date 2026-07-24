#!/usr/bin/env python3
"""Discover CPython's C-accelerator <-> pure-Python-twin module pairs.

CPython ships several stdlib modules *twice*: a fast C accelerator and a pure
Python implementation of the same public API. The pure-Python twin is a free
differential oracle -- feed the same adversarial input to both backends and any
divergence (a C-side segfault while the twin raises a clean exception, or two
different exception types) is a localized, confirmed lead.

This is a **discovery** script, not a bug scanner. It walks a CPython checkout
and emits the inventory of dual implementations so the ``parity-checker`` agent
knows what to differentially test. It reports, per pair:

* the public module name (``decimal``, ``io``, ``heapq`` ...),
* the pure-Python implementation path,
* the C accelerator source path(s),
* how the pair was detected, and
* a confidence in the pairing.

Two detection methods are used:

1. **explicit ``_py*`` twin** -- a ``Lib/_py<name>.py`` file (e.g.
   ``_pydecimal.py``, ``_pyio.py``, ``_pydatetime.py``). These are the
   strongest, unambiguous dual implementations.
2. **accelerator-import** -- a public module ``Lib/<name>.py`` (or a package
   ``Lib/<name>/``) that imports its same-named C accelerator ``_<name>``
   (``from _heapq import *``, ``from _json import make_scanner``, ...). The
   pure-Python fallback lives inline in the public module.

A pair discovered by *both* methods (``decimal``, ``io``, ``datetime`` ...) is
merged and reported once.

Usage:
    python find_parity_pairs.py [path-to-cpython-checkout] [--max-files N]
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_common import (
    build_report,
    parse_common_args,
    relpath,
    resolve_roots,
)

# Confidence ordering used to sort the inventory (highest first).
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}

# A comment line never carries a real import; skip it during import detection.
_COMMENT_LINE = re.compile(r"^\s*#")


def _c_module_and_base(twin_stem: str) -> tuple[str, str]:
    """Map a ``_py*`` twin stem to its C accelerator module name and base.

    ``_pydecimal`` -> (``_decimal``, ``decimal``);
    ``_py_abc``    -> (``_abc``, ``abc``);
    ``_py_warnings`` -> (``_warnings``, ``warnings``).
    """
    rest = twin_stem[len("_py") :]
    if rest.startswith("_"):
        # Twin name already carries the accelerator's leading underscore.
        return rest, rest[1:]
    return "_" + rest, rest


def _locate_c_sources(project_root: Path, c_module: str, base: str) -> list[str]:
    """Return the C accelerator source path(s) for module ``c_module``.

    Searches the conventional CPython layouts, in order:

    * ``Modules/<c_module>/*.c``      (package accelerators: ``_decimal``, ``_io``)
    * ``Modules/<c_module>module.c``  (``_datetimemodule.c``, ``_heapqmodule.c``)
    * ``Modules/<c_module>.c``        (``_json.c``, ``_csv.c``, ``_pickle.c``)
    * ``Python/<c_module>.c``         (``_warnings.c``)
    * ``Objects/<base>object.c``      (last-resort; e.g. ``_pylong`` -> longobject.c)
    """
    sources: list[Path] = []
    modules = project_root / "Modules"

    pkg_dir = modules / c_module
    if pkg_dir.is_dir():
        # Non-recursive: skip vendored subtrees like Modules/_decimal/libmpdec.
        sources.extend(sorted(pkg_dir.glob("*.c")))

    for candidate in (
        modules / f"{c_module}module.c",
        modules / f"{c_module}.c",
        project_root / "Python" / f"{c_module}.c",
    ):
        if candidate.is_file():
            sources.append(candidate)

    if not sources:
        fallback = project_root / "Objects" / f"{base}object.c"
        if fallback.is_file():
            sources.append(fallback)

    return [relpath(s, project_root) for s in sources]


def _module_python_files(lib: Path, name: str) -> list[Path]:
    """Return the Python file(s) making up public module ``name``.

    A single-file module is ``Lib/<name>.py``; a package is every ``*.py`` one
    level deep in ``Lib/<name>/`` (enough to reach the accelerator imports that
    live in package submodules, e.g. ``Lib/json/scanner.py``).
    """
    single = lib / f"{name}.py"
    if single.is_file():
        return [single]
    pkg = lib / name
    if (pkg / "__init__.py").is_file():
        return sorted(pkg.glob("*.py"))
    return []


def _detect_accelerator_import(files: list[Path], base: str) -> tuple[str, list[Path]]:
    """Detect whether ``files`` import the ``_<base>`` C accelerator.

    Returns ``(import_style, import_sites)`` where ``import_style`` is
    ``"star"`` (``from _base import *`` -- a full replacement), ``"named"``
    (a selective ``from _base import name`` / ``import _base``), or ``"none"``.
    """
    star_re = re.compile(rf"from\s+_{re.escape(base)}\s+import\s+\*")
    from_re = re.compile(rf"from\s+_{re.escape(base)}\s+import\b")
    import_re = re.compile(rf"(?:^|;)\s*import\s+_{re.escape(base)}\b")

    style = "none"
    sites: list[Path] = []
    for filepath in files:
        try:
            text = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hit = False
        for line in text.splitlines():
            if _COMMENT_LINE.match(line):
                continue
            if star_re.search(line):
                style = "star"
                hit = True
            elif from_re.search(line) or import_re.search(line):
                if style == "none":
                    style = "named"
                hit = True
        if hit:
            sites.append(filepath)
    return style, sites


def _confidence(detection: str, import_style: str, c_sources: list[str]) -> str:
    """Grade the confidence of a discovered pair."""
    if detection in ("explicit_py_twin", "both"):
        # A dedicated _py* twin file is definitive evidence of a dual impl;
        # only downgrade if we could not locate the C side to test against.
        return "high" if c_sources else "medium"
    # accelerator-import only: a star import replaces the whole module (strong),
    # a selective import accelerates only some primitives (weaker).
    return "medium" if import_style == "star" else "low"


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Discover C-accelerator / pure-Python-twin module pairs in a checkout."""
    project_root, scan_root = resolve_roots(target)
    lib = project_root / "Lib"

    pairs: dict[str, dict] = {}
    files_examined = 0
    skipped: list[dict] = []

    if not lib.is_dir():
        skipped.append(
            {"file": str(lib), "reason": "no Lib/ directory under project root"}
        )

    # --- Pass 1: explicit _py* twins ---------------------------------------
    if lib.is_dir():
        for twin in sorted(lib.glob("_py*.py")):
            files_examined += 1
            c_module, base = _c_module_and_base(twin.stem)
            c_sources = _locate_c_sources(project_root, c_module, base)
            pairs[base] = {
                "type": "parity_pair",
                "module": base,
                "python_impl": relpath(twin, project_root),
                "python_twin_module": twin.stem,
                "c_module": c_module,
                "c_sources": c_sources,
                "detection": "explicit_py_twin",
                "import_style": "none",
                "import_sites": [],
                "force_python_hint": f"import {twin.stem}",
            }

    # --- Pass 2: accelerator imports in public modules ---------------------
    if lib.is_dir():
        candidates: set[str] = set()
        for entry in sorted(lib.iterdir()):
            if entry.name.startswith("_"):
                continue
            if entry.is_file() and entry.suffix == ".py":
                candidates.add(entry.stem)
            elif entry.is_dir() and (entry / "__init__.py").is_file():
                candidates.add(entry.name)

        for name in sorted(candidates):
            c_module = "_" + name
            # Cheap gate: only inspect a module's Python source if a same-named
            # C accelerator actually exists (avoids reading all of Lib/test/).
            c_sources = _locate_c_sources(project_root, c_module, name)
            if not c_sources:
                continue
            files = _module_python_files(lib, name)
            if not files:
                continue
            if max_files and files_examined >= max_files:
                break
            files_examined += len(files)
            import_style, sites = _detect_accelerator_import(files, name)
            if import_style == "none":
                continue
            site_rel = [relpath(s, project_root) for s in sites]

            existing = pairs.get(name)
            if existing is not None:
                # Already found as a _py* twin -> upgrade to "both" and enrich.
                existing["detection"] = "both"
                existing["import_style"] = import_style
                existing["import_sites"] = site_rel
                existing["python_dispatcher"] = site_rel[0] if site_rel else None
                if not existing["c_sources"]:
                    existing["c_sources"] = c_sources
            else:
                entry_point = files[0]
                pairs[name] = {
                    "type": "parity_pair",
                    "module": name,
                    "python_impl": relpath(entry_point, project_root),
                    "python_twin_module": None,
                    "c_module": c_module,
                    "c_sources": c_sources,
                    "detection": "accelerator_import",
                    "import_style": import_style,
                    "import_sites": site_rel,
                    "force_python_hint": (
                        f"block '{c_module}' in sys.modules before importing "
                        f"{name} (the C accelerator shadows the pure-Python "
                        "definitions in the same file)"
                    ),
                }

    # --- Finalize ----------------------------------------------------------
    findings: list[dict] = []
    for pair in pairs.values():
        pair["confidence"] = _confidence(
            pair["detection"], pair["import_style"], pair["c_sources"]
        )
        findings.append(pair)

    findings.sort(key=lambda p: (_CONFIDENCE_RANK.get(p["confidence"], 9), p["module"]))

    by_confidence: dict[str, int] = defaultdict(int)
    by_detection: dict[str, int] = defaultdict(int)
    for pair in findings:
        by_confidence[pair["confidence"]] += 1
        by_detection[pair["detection"]] += 1

    return build_report(
        project_root=project_root,
        scan_root=scan_root,
        files_analyzed=files_examined,
        functions_analyzed=0,
        findings=findings,
        summary={
            "total_pairs": len(findings),
            "by_confidence": dict(by_confidence),
            "by_detection": dict(by_detection),
        },
        skipped_files=skipped,
    )


def main() -> None:
    try:
        target, max_files = parse_common_args(sys.argv[1:])
        result = analyze(target, max_files=max_files)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:  # noqa: BLE001 - top-level guard emits JSON error
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()

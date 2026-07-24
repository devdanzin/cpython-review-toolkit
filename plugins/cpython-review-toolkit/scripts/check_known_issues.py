#!/usr/bin/env python3
"""Cross-reference the known-CPython-crash catalog against a fresh scan.

Backs the ``known-issues`` command. ``data/cpython_known_bugs.tsv`` records
crash sites previously confirmed in CPython — from the findings repos
(``OOM-####`` / ``TSAN-####``) and the upstream tracker (``gh-NNNNNN``). Each
row names a repo-relative C path, an enclosing function, a category, and (when
known) a line. This script runs the category's matching scanner over each
referenced file and reports, per catalog entry, whether the site is:

  * ``present``      — a finding of that category at (or within a small drift
                       window of) the catalog line, or — when the catalog line
                       is unknown (0) — a finding in the named function.
  * ``line_drifted`` — the file still has findings of that category, but not at
                       the catalog line, and the catalog's named function is
                       *gone* (renamed/removed) or unknown; ``nearest_line``
                       points at the closest finding. The bug likely just moved.
  * ``absent_in_function``
                     — the catalog names a function, that function still exists
                       in the file, and it carries no finding of the category.
                       The file has findings elsewhere, but not here. This is
                       absence *in the function*, not drift: drift means "still
                       there, moved"; this means "possibly fixed".
  * ``absent``       — the file was scanned and has no findings of that
                       category anywhere (likely fixed — but see the caveat).
  * ``file_missing`` — the path no longer exists under the target checkout.
  * ``no_scanner``   — the category has no scanner (as of 0.7, none); carried
                       through so the catalog stays complete, but not
                       cross-referenced.

This is a static, drift-tolerant regression baseline — it does NOT run repros.

CAVEAT: an ``absent`` verdict is NOT proof of a fix. Some crash shapes carry no
scannable token at the exact site — a native C-stack overflow (recursion), a
data race (tsan) — so a fresh scan can read a still-unfixed bug as ``absent``.
Always read the file before concluding a bug is fixed.

Usage:
    python check_known_issues.py [path] [--max-files N] [--catalog FILE]
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_ft_races
import scan_init_bypass
import scan_memory_patterns
import scan_null_checks
import scan_pyerr_clear
import scan_recursion_guards
import scan_refcounts
import scan_uninit_dealloc

# Regex-based C function boundaries (no tree-sitter): the tree-sitter chassis
# silently merges and drops functions on CPython's brace-unbalanced macros,
# which would make "the function is gone" a false conclusion.
from analyze_history import get_c_function_boundaries
from scan_common import build_report, parse_common_args, resolve_roots

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DEFAULT_CATALOG = _DATA_DIR / "cpython_known_bugs.tsv"

# Category -> scanner module. A category present in the catalog but missing
# here (or mapped to ``None``) is carried through as ``no_scanner`` so the
# catalog stays complete. As of 0.7 every category has a scanner.
CATEGORY_SCANNERS: dict[str, object] = {
    "recursion": scan_recursion_guards,
    "pyerr-clear": scan_pyerr_clear,
    "uninit-dealloc": scan_uninit_dealloc,
    "null-deref": scan_null_checks,
    "refcount": scan_refcounts,
    "tsan": scan_ft_races,
    "init-bypass": scan_init_bypass,
    # Allocation-size overflow, OOB member walks, GC-track invariants. Wired in
    # when the findings catalog gained memory-pattern records: without it those
    # entries report `no_scanner`, and filing them under a category whose
    # scanner cannot express an allocation multiply would yield a misleading
    # `absent` verdict instead.
    "memory-pattern": scan_memory_patterns,
}

# ± lines tolerated around a catalog line before a match is called "drifted".
_DRIFT = 5

_ABSENCE_CAVEAT = (
    "An `absent` verdict is NOT proof of a fix. Some crash shapes carry no "
    "scannable token at the exact site — a native C-stack overflow (recursion), "
    "a data race (tsan) — so a fresh scan can read a still-unfixed bug as "
    "`absent`. Always read the file before concluding a bug is fixed."
)
_ABSENT_IN_FUNCTION_CAVEAT = (
    "`absent_in_function` is NOT `line_drifted`. It means the catalog's named "
    "function is still in the file and carries no finding of the category, "
    'while the file has findings elsewhere. Drift says "still there, moved"; '
    'this says "possibly fixed in this function" — a materially weaker '
    "regression signal, so these are reported but do not become findings. "
    "Read the function before concluding either way."
)
_NO_SCANNER_CAVEAT = (
    "Every catalog category now has a scanner (as of 0.7). A category added to "
    "the catalog without a matching entry in CATEGORY_SCANNERS is carried "
    "through as `no_scanner` (informational) so the catalog stays complete, but "
    "is not cross-referenced against a fresh scan. Note the caveat above still "
    "applies to the scanned categories: a data race (tsan) or a native "
    "stack overflow (recursion) carries no scannable token at many sites, so "
    "`absent` there is not proof of a fix."
)


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------


def _load_catalog(path: Path) -> list[dict]:
    """Parse the TAB-separated bug catalog into a list of entry dicts.

    Columns: ``bug_id  path  line  category  function  note`` (note optional,
    line may be ``0`` for "unknown — match by function/category").
    """
    entries: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return entries
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 5:
            continue
        bug_id, rel_path, line_s, category, function = (p.strip() for p in parts[:5])
        note = "\t".join(parts[5:]).strip() if len(parts) > 5 else ""
        try:
            line = int(line_s)
        except ValueError:
            line = 0
        entries.append(
            {
                "bug_id": bug_id,
                "path": rel_path,
                "line": line,
                "category": category,
                "function": function,
                "note": note,
            }
        )
    return entries


# ---------------------------------------------------------------------------
# Single-file scanning
# ---------------------------------------------------------------------------


def _scan_file(scanner, abs_file: Path, max_files: int) -> list[dict]:
    """Run ``scanner`` over exactly one file, returning its findings.

    The scanners resolve a *file* target to its parent directory (via
    ``resolve_roots`` or an inline equivalent), so ``analyze(<file>)`` would
    scan the whole directory — and a small ``--max-files`` could truncate
    before reaching the target file. We instead patch the module-global
    ``discover_c_files`` for the duration of the call so the scan iterates
    exactly this one file. Project-root detection (hence the relative finding
    paths we match on) still works, because we pass the absolute file path as
    the target and the scanner walks up to the CPython root itself.
    """

    def _one_file(_root, *, max_files: int = 0):
        yield abs_file

    original = scanner.discover_c_files
    scanner.discover_c_files = _one_file
    try:
        report = scanner.analyze(str(abs_file), max_files=max_files)
    finally:
        scanner.discover_c_files = original
    return report.get("findings", [])


def _index_findings(findings: list[dict], rel_file: str, abs_file: Path) -> dict:
    """Index a file's findings by line-set, by function, and by function range.

    ``func_ranges`` records every function the file *defines* (regex-based, so
    it is independent of the tree-sitter chassis). It answers the question
    ``line_drifted`` used to conflate: is the catalog's function still here?
    """
    lines: set[int] = set()
    by_function: dict[str, list[int]] = defaultdict(list)
    for f in findings:
        if f.get("file") != rel_file:
            continue  # defensive: only this file's findings
        line = int(f.get("line", 0) or 0)
        lines.add(line)
        by_function[f.get("function", "")].append(line)

    func_ranges: dict[str, tuple[int, int]] = {}
    for func in get_c_function_boundaries(abs_file):
        start, end = func["line_start"], func["line_end"]
        existing = func_ranges.get(func["name"])
        # Keep the widest span if a name appears more than once (#ifdef arms).
        if existing is None:
            func_ranges[func["name"]] = (start, end)
        else:
            func_ranges[func["name"]] = (
                min(existing[0], start),
                max(existing[1], end),
            )
    return {
        "lines": lines,
        "by_function": dict(by_function),
        "func_ranges": func_ranges,
    }


def _function_hits(entry_function: str, index: dict) -> list[int]:
    """Findings attributable to ``entry_function``, by name *or* by line range.

    Name-matching alone under-reports: the tree-sitter chassis can attribute a
    finding to a merged/neighbouring function, so a finding physically inside
    the function body still counts.
    """
    hits = list(index["by_function"].get(entry_function, []))
    span = index["func_ranges"].get(entry_function)
    if span:
        start, end = span
        hits.extend(ln for ln in index["lines"] if start <= ln <= end)
    return sorted(set(hits))


def _classify(entry: dict, index: dict) -> tuple[str, int | None]:
    """Return ``(status, matched_line)`` for a scanned catalog entry."""
    lines: set[int] = index["lines"]
    if not lines:
        return "absent", None

    func = entry["function"]
    named = bool(func) and func != "0"
    func_hits = _function_hits(func, index) if named else []
    func_exists = named and func in index["func_ranges"]
    cat_line = entry["line"]

    if cat_line > 0:
        nearest = min(lines, key=lambda ln: abs(ln - cat_line))
        if any(abs(ln - cat_line) <= _DRIFT for ln in lines):
            return "present", nearest
        if func_hits:
            return "present", min(func_hits)
        # The catalog names a function that is still here and clean: that is
        # absence in the function, not drift.
        if func_exists:
            return "absent_in_function", None
        return "line_drifted", nearest

    # Catalog line unknown: match by function name when we have one.
    if named:
        if func_hits:
            return "present", min(func_hits)
        if func_exists:
            return "absent_in_function", None
        return "line_drifted", min(lines)
    # No line and no function: any finding of the category counts as present.
    return "present", min(lines)


# ---------------------------------------------------------------------------
# Analysis entry point
# ---------------------------------------------------------------------------


def analyze(
    target: str,
    *,
    max_files: int = 0,
    catalog_path: str | None = None,
) -> dict:
    """Cross-reference the known-crash catalog against a fresh scan of target."""
    project_root, scan_root = resolve_roots(target)
    catalog_file = Path(catalog_path) if catalog_path else _DEFAULT_CATALOG
    catalog = _load_catalog(catalog_file)

    # Cache one index per (category, rel_file) so multi-site bugs and repeated
    # files scan each file at most once per scanner.
    index_cache: dict[tuple[str, str], dict] = {}
    scanned_files: set[str] = set()

    results: list[dict] = []
    findings: list[dict] = []
    status_counts: dict[str, int] = {
        "present": 0,
        "line_drifted": 0,
        "absent_in_function": 0,
        "absent": 0,
        "file_missing": 0,
        "no_scanner": 0,
    }
    per_bug: dict[str, list[str]] = defaultdict(list)

    for entry in catalog:
        category = entry["category"]
        rel_file = entry["path"]
        scanner = CATEGORY_SCANNERS.get(category)
        matched_line: int | None = None

        if scanner is None:
            status = "no_scanner"
        else:
            abs_file = project_root / rel_file
            if not abs_file.is_file():
                status = "file_missing"
            else:
                key = (category, rel_file)
                if key not in index_cache:
                    scan_findings = _scan_file(scanner, abs_file, max_files)
                    index_cache[key] = _index_findings(
                        scan_findings,
                        rel_file,
                        abs_file,
                    )
                    scanned_files.add(rel_file)
                status, matched_line = _classify(entry, index_cache[key])

        status_counts[status] += 1
        per_bug[entry["bug_id"]].append(status)

        results.append(
            {
                "bug_id": entry["bug_id"],
                "path": rel_file,
                "catalog_line": entry["line"],
                "category": category,
                "function": entry["function"],
                "note": entry["note"],
                "status": status,
                "nearest_line": matched_line,
                "scanner": getattr(scanner, "__name__", None),
            }
        )

        if status in ("present", "line_drifted"):
            confidence = "high" if status == "present" else "medium"
            where = (
                f"'{entry['function']}'"
                if entry["function"] not in ("", "0")
                else rel_file
            )
            if status == "present":
                detail = (
                    f"{entry['bug_id']}: known {category} bug in {where} ({rel_file}) "
                    "is still present in a fresh scan — a previously-confirmed CPython "
                    "crash site. Treat as a regression signal and read the file."
                )
            else:
                detail = (
                    f"{entry['bug_id']}: known {category} bug in {where} ({rel_file}) — "
                    f"catalog site drifted; the file still has {category} findings, "
                    f"nearest at line {matched_line}. The bug likely just moved; verify."
                )
            findings.append(
                {
                    "type": "known_bug_present"
                    if status == "present"
                    else "known_bug_line_drifted",
                    "file": rel_file,
                    "function": entry["function"],
                    "line": matched_line or entry["line"] or 0,
                    "confidence": confidence,
                    "detail": detail,
                    "bug_id": entry["bug_id"],
                    "category": category,
                    "status": status,
                    "catalog_line": entry["line"],
                    "nearest_line": matched_line,
                    "note": entry["note"],
                }
            )

    # Per-bug rollup, strongest signal wins: still-there > moved >
    # gone-from-this-function > gone-from-the-file.
    bug_rollup: dict[str, str] = {}
    for bug_id, statuses in per_bug.items():
        s = set(statuses)
        if "present" in s:
            bug_rollup[bug_id] = "present"
        elif "line_drifted" in s:
            bug_rollup[bug_id] = "line_drifted"
        elif "absent_in_function" in s:
            bug_rollup[bug_id] = "absent_in_function"
        elif s & {"absent", "file_missing"}:
            bug_rollup[bug_id] = "likely_fixed"
        else:
            bug_rollup[bug_id] = "no_scanner"

    rollup_counts: dict[str, int] = defaultdict(int)
    for verdict in bug_rollup.values():
        rollup_counts[verdict] += 1

    return build_report(
        project_root=project_root,
        scan_root=scan_root,
        files_analyzed=len(scanned_files),
        functions_analyzed=len(catalog),
        findings=findings,
        summary={
            "total_findings": len(findings),
            "still_present": status_counts["present"],
            "line_drifted": status_counts["line_drifted"],
            "absent_in_function": status_counts["absent_in_function"],
            "no_scanner": status_counts["no_scanner"],
            "by_status": dict(status_counts),
        },
        catalog_summary={
            "catalog_path": str(catalog_file),
            "catalog_entries": len(catalog),
            "distinct_bugs": len(per_bug),
            "files_scanned": len(scanned_files),
            "status_counts": dict(status_counts),
            "rollup_counts": dict(rollup_counts),
        },
        bug_rollup=dict(sorted(bug_rollup.items())),
        catalog_results=results,
        notes=[_ABSENCE_CAVEAT, _ABSENT_IN_FUNCTION_CAVEAT, _NO_SCANNER_CAVEAT],
    )


def main() -> None:
    try:
        argv = sys.argv[1:]
        catalog_override: str | None = None
        filtered: list[str] = []
        i = 0
        while i < len(argv):
            if argv[i] == "--catalog" and i + 1 < len(argv):
                catalog_override = argv[i + 1]
                i += 2
                continue
            filtered.append(argv[i])
            i += 1
        target, max_files = parse_common_args(filtered)
        result = analyze(target, max_files=max_files, catalog_path=catalog_override)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:  # noqa: BLE001 -- top-level guard, emit JSON error
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()

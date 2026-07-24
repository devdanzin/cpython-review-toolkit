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
                       the catalog line / named function; ``nearest_line``
                       points at the closest one. The bug likely just moved.
  * ``absent``       — the file was scanned and has no findings of that
                       category (likely fixed — but see the caveat below).
  * ``file_missing`` — the path no longer exists under the target checkout.
  * ``no_scanner``   — the category has no scanner in v0.5 (``tsan`` /
                       ``init-bypass``); carried through so the catalog stays
                       complete, but not cross-referenced.

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
import scan_null_checks
import scan_pyerr_clear
import scan_recursion_guards
import scan_refcounts
import scan_uninit_dealloc
from scan_common import build_report, parse_common_args, resolve_roots

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DEFAULT_CATALOG = _DATA_DIR / "cpython_known_bugs.tsv"

# Category -> scanner module. ``None`` means "no scanner exists yet"; such
# entries are carried through as ``no_scanner`` so the catalog stays complete.
CATEGORY_SCANNERS: dict[str, object] = {
    "recursion": scan_recursion_guards,
    "pyerr-clear": scan_pyerr_clear,
    "uninit-dealloc": scan_uninit_dealloc,
    "null-deref": scan_null_checks,
    "refcount": scan_refcounts,
    "tsan": scan_ft_races,
    "init-bypass": None,
}

# ± lines tolerated around a catalog line before a match is called "drifted".
_DRIFT = 5

_ABSENCE_CAVEAT = (
    "An `absent` verdict is NOT proof of a fix. Some crash shapes carry no "
    "scannable token at the exact site — a native C-stack overflow (recursion), "
    "a data race (tsan) — so a fresh scan can read a still-unfixed bug as "
    "`absent`. Always read the file before concluding a bug is fixed."
)
_NO_SCANNER_CAVEAT = (
    "Category `init-bypass` has no scanner yet; its entries are carried through "
    "as `no_scanner` (informational) so the catalog stays complete, but they are "
    "not cross-referenced against a fresh scan. (`tsan` is now scanned by "
    "scan_ft_races, but note the caveat above: a data race carries no scannable "
    "token at many sites, so `absent` there still is not proof of a fix.)"
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


def _index_findings(findings: list[dict], rel_file: str) -> dict:
    """Index a file's findings by line-set and by enclosing function."""
    lines: set[int] = set()
    by_function: dict[str, list[int]] = defaultdict(list)
    for f in findings:
        if f.get("file") != rel_file:
            continue  # defensive: only this file's findings
        line = int(f.get("line", 0) or 0)
        lines.add(line)
        by_function[f.get("function", "")].append(line)
    return {"lines": lines, "by_function": dict(by_function)}


def _classify(entry: dict, index: dict) -> tuple[str, int | None]:
    """Return ``(status, matched_line)`` for a scanned catalog entry."""
    lines: set[int] = index["lines"]
    by_function: dict[str, list[int]] = index["by_function"]
    if not lines:
        return "absent", None

    func = entry["function"]
    func_hits = by_function.get(func) if func and func != "0" else None
    cat_line = entry["line"]

    if cat_line > 0:
        nearest = min(lines, key=lambda ln: abs(ln - cat_line))
        if any(abs(ln - cat_line) <= _DRIFT for ln in lines):
            return "present", nearest
        if func_hits:
            return "present", min(func_hits)
        return "line_drifted", nearest

    # Catalog line unknown: match by function name when we have one.
    if func and func != "0":
        if func_hits:
            return "present", min(func_hits)
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
                    index_cache[key] = _index_findings(scan_findings, rel_file)
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

    # Per-bug rollup: a bug is only "likely_fixed" if none of its sites is
    # present or drifted and at least one scanned/known site is clean or gone.
    bug_rollup: dict[str, str] = {}
    for bug_id, statuses in per_bug.items():
        s = set(statuses)
        if "present" in s:
            bug_rollup[bug_id] = "present"
        elif "line_drifted" in s:
            bug_rollup[bug_id] = "line_drifted"
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
        notes=[_ABSENCE_CAVEAT, _NO_SCANNER_CAVEAT],
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

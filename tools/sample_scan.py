#!/usr/bin/env python3
"""Run a scanner over an explicit file list and keep every number sample-scoped.

Why this exists
---------------

A review run that focuses on a subset of a directory ("the 12-file `Modules/`
sample") needs per-scanner JSON for *that subset*. The obvious way to get it --
scan the whole directory, then filter ``findings[]`` down to the sample -- is
wrong in a specific, dangerous way: it leaves every **denominator** at its
corpus-wide value.

That is measured, not hypothetical. ``scan_init_bypass.sample.json`` from the
`Modules/` run shipped ``findings: []`` next to ``total_nullable_fields: 103``
and ``files_with_nullable_fields: 10`` -- numbers describing all 380 files of
``Modules/``, sitting beside a zero describing 12. The run brief tells agents
exactly the right thing ("a zero with a zero denominator is silence, not
safety"), and the filtered file inverts that canary: it makes an *unearned*
zero look earned, because the denominator says 103 fields were examined.

The fix is not a smarter filter. A corpus-wide denominator cannot be recomputed
from findings alone -- nothing in ``findings[]`` records how many nullable
fields the scanner looked at. So this tool does the only thing that is actually
correct: it **re-runs the scanner over exactly the sample files** and merges the
per-file reports, so every count in the output was produced by the sample.

Usage
-----

    python tools/sample_scan.py scan_init_bypass /path/to/cpython \\
        --files Modules/_json.c Modules/_pickle.c

    python tools/sample_scan.py scan_deprecated_apis /path/to/cpython \\
        --files-from sample.txt -o scanners/scan_deprecated_apis.sample.json

The merge policy is explicit and recorded in the output under ``_sample`` so a
reader can audit it: numeric values are summed, dicts merged key-wise, lists
concatenated, strings carried through, and the handful of keys that are
constants rather than counts (``apis_in_vocabulary``) are carried, not summed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

_SCRIPTS_DIR = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "cpython-review-toolkit"
    / "scripts"
)

# Values that describe the *scanner*, not the corpus: summing them is nonsense.
_CARRY_NUMERIC = frozenset({"apis_in_vocabulary"})

# Values that identify the run rather than count anything.
_CARRY_ALWAYS = frozenset({"project_root", "scan_root", "vocabulary_source"})


def load_scanner(name_or_path: str):
    """Import a scanner module by bare name or by path."""
    path = Path(name_or_path)
    if not path.suffix:
        path = _SCRIPTS_DIR / f"{name_or_path}.py"
    if not path.is_file():
        raise SystemExit(f"no such scanner: {path}")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "analyze"):
        raise SystemExit(f"{path} has no analyze() entry point")
    return module


def merge_values(key: str, values: list, policy: dict[str, str]):
    """Merge one envelope key across per-file reports, recording the policy."""
    values = [v for v in values if v is not None]
    if not values:
        return None
    first = values[0]
    # Nested keys arrive dotted ("summary.apis_in_vocabulary"); match on the
    # leaf so a constant stays a constant wherever it is nested.
    leaf = key.rpartition(".")[2]
    if leaf in _CARRY_ALWAYS or leaf in _CARRY_NUMERIC:
        policy.setdefault(key, "carried (constant, not a count)")
        return first
    if isinstance(first, bool):
        policy.setdefault(key, "or")
        return any(bool(v) for v in values)
    if isinstance(first, (int, float)):
        policy.setdefault(key, "summed")
        return sum(values)
    if isinstance(first, dict):
        policy.setdefault(key, "merged key-wise")
        merged: dict = {}
        for value in values:
            for sub_key, sub_value in value.items():
                if sub_key in merged:
                    merged[sub_key] = merge_values(
                        f"{key}.{sub_key}", [merged[sub_key], sub_value], policy
                    )
                else:
                    merged[sub_key] = sub_value
        return merged
    if isinstance(first, list):
        policy.setdefault(key, "concatenated")
        out: list = []
        for value in values:
            out.extend(value)
        return out
    policy.setdefault(key, "carried (first)")
    return first


def merge_reports(reports: list[dict]) -> tuple[dict, dict[str, str]]:
    """Merge per-file scanner reports into one sample-scoped envelope."""
    policy: dict[str, str] = {}
    keys: list[str] = []
    for report in reports:
        for key in report:
            if key not in keys:
                keys.append(key)
    merged: dict = {}
    for key in keys:
        merged[key] = merge_values(key, [r.get(key) for r in reports], policy)
    merged.setdefault("findings", [])
    merged["findings"].sort(key=lambda f: (f.get("file", ""), f.get("line", 0)))
    return merged, policy


def sample_scan(scanner, root: Path, files: list[str]) -> dict:
    """Run ``scanner.analyze`` once per sample file and merge the results."""
    reports: list[dict] = []
    scanned: list[str] = []
    missing: list[str] = []
    for rel in files:
        path = root / rel
        if not path.is_file():
            missing.append(rel)
            continue
        reports.append(scanner.analyze(str(path)))
        scanned.append(rel)
    if not reports:
        raise SystemExit("no sample file exists under the given root")

    merged, policy = merge_reports(reports)
    merged["scan_root"] = str(root)
    merged["_sample"] = {
        "files_requested": list(files),
        "files_scanned": scanned,
        "files_missing": missing,
        "method": (
            "the scanner was RE-RUN over exactly these files; every count in "
            "this report is sample-scoped. This is not a post-hoc filter of a "
            "corpus-wide scan -- a filtered report keeps corpus-wide "
            "denominators next to sample-scoped findings, which makes an "
            "unearned zero look earned."
        ),
        "merge_policy": policy,
    }
    return merged


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("scanner", help="scanner name (scan_refcounts) or path")
    ap.add_argument("root", help="project root the file list is relative to")
    ap.add_argument("--files", nargs="+", default=[], help="sample files")
    ap.add_argument("--files-from", help="file with one sample path per line")
    ap.add_argument("-o", "--output", help="write JSON here instead of stdout")
    args = ap.parse_args(argv)

    files = list(args.files)
    if args.files_from:
        with open(args.files_from, encoding="utf-8") as fh:
            files.extend(
                line.strip() for line in fh if line.strip() and not line.startswith("#")
            )
    if not files:
        ap.error("give --files or --files-from")

    report = sample_scan(load_scanner(args.scanner), Path(args.root).resolve(), files)
    text = json.dumps(report, indent=2, default=str) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

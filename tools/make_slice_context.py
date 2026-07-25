#!/usr/bin/env python3
"""Generate the run directory and RUN_CONTEXT.md for one review slice.

Why this exists
---------------

The two informed-explore runs that produced every finding so far were both set
up by hand: create the run tree, run thirteen scanners twice (once corpus-wide,
once sample-scoped), work out which files carry catalog entries and which are
new territory, and write the brief the agents read. That is an hour of careful
clerical work per slice, and the campaign has 35 slices left. It is also where
a mistake is most expensive -- the `Modules/` run shipped filtered scanner JSON
whose denominators were corpus-wide, which made unearned zeros look earned.

So this does it mechanically, and gets the two subtle parts right by
construction:

* **Sample scans are re-run, never filtered.** Delegated to
  ``tools/sample_scan.py``, so every count in ``*.sample.json`` is
  sample-scoped. See that module's docstring for the measurement.
* **The calibration/new-territory split is derived**, not asserted -- a slice
  file is calibration if it appears in ``cpython_known_bugs.tsv`` or cites a
  finding in the catalog. That split is what tells an agent whether a clean
  result on a file is a recall failure or a real negative.

Corpus-wide baselines are cached per (scanner, top-level directory) under
``<reports>/_baseline/`` and reused by every slice in that directory. The whole
campaign therefore pays for two full sweeps -- one ``Objects/``, one
``Modules/`` -- rather than one per slice: 85s the first time, under a second
after that.

Usage
-----

    python tools/make_slice_context.py mod-io --cpython ~/projects/cpython \\
        --catalog-dir ~/projects/cpython-review-findings
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO / "plugins" / "cpython-review-toolkit" / "scripts"
_DATA = _REPO / "plugins" / "cpython-review-toolkit" / "data"
_DOC = __doc__ or ""

SCANNERS = [
    "scan_deprecated_apis",
    "scan_error_paths",
    "scan_ft_races",
    "scan_gil_usage",
    "scan_init_bypass",
    "scan_lock_discipline",
    "scan_memory_patterns",
    "scan_null_checks",
    "scan_pyerr_clear",
    "scan_recursion_guards",
    "scan_refcounts",
    "scan_stw_safety",
    "scan_uninit_dealloc",
]


_QUIET = False


def _log(message: str) -> None:
    """Progress goes to stderr so stdout stays free for machine consumption."""
    if not _QUIET:
        print(message, file=sys.stderr)


def _import(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def corpus_of(spec: dict) -> str:
    """The top-level directory a slice's findings should be compared against.

    Not the slice's own ``scope``: for a package slice like ``mod-io`` the scope
    is ``Modules/_io``, so a scope-wide baseline is the slice itself and the
    comparison says nothing. Comparing against all of ``Modules/`` is what
    answers the question the column exists for -- is this rule noisy *here*, or
    noisy everywhere? The baseline is cached, so the whole campaign pays for it
    twice: once for ``Objects``, once for ``Modules``.
    """
    tops = {f.split("/", 1)[0] for f in spec["files"]}
    if len(tops) != 1:
        raise SystemExit(f"slice spans multiple top-level dirs: {sorted(tops)}")
    return tops.pop()


def known_bug_files(tsv: Path) -> dict[str, list[str]]:
    """Map CPython source path -> the bug ids the regression catalog records."""
    out: dict[str, list[str]] = {}
    if not tsv.is_file():
        return out
    with open(tsv, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip() or line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) >= 2:
                out.setdefault(cols[1], []).append(cols[0])
    return out


def catalog_files(catalog_dir: Path | None) -> dict[str, list[str]]:
    """Map CPython source path -> finding ids in the findings repo."""
    out: dict[str, list[str]] = {}
    if not catalog_dir:
        return out
    for meta_path in sorted((catalog_dir / "reports").glob("*/meta.json")):
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        fid = meta.get("id", meta_path.parent.name)
        for site in meta.get("sites", []):
            path = site.get("path")
            if path and fid not in out.setdefault(path, []):
                out[path].append(fid)
    return out


def run_baselines(
    sample_scan, scope_root: Path, scope: str, baseline_dir: Path, refresh: bool
) -> dict[str, dict]:
    """Scan the slice's parent scope once per scanner, cached on disk."""
    baseline_dir.mkdir(parents=True, exist_ok=True)
    key = scope.replace("/", "_")
    out: dict[str, dict] = {}
    for name in SCANNERS:
        cached = baseline_dir / f"{name}.{key}.json"
        if cached.is_file() and not refresh:
            with open(cached, encoding="utf-8") as fh:
                out[name] = json.load(fh)
            _log(f"  {name:26s} baseline cached")
            continue
        started = time.monotonic()
        module = sample_scan.load_scanner(name)
        report = module.analyze(str(scope_root))
        cached.write_text(
            json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
        )
        out[name] = report
        _log(
            f"  {name:26s} baseline {len(report.get('findings', [])):5d} "
            f"({time.monotonic() - started:.1f}s)"
        )
    return out


def run_samples(
    sample_scan, cpython: Path, files: list[str], out_dir: Path
) -> dict[str, dict]:
    """Re-run each scanner over exactly the slice files (never a filter)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, dict] = {}
    for name in SCANNERS:
        module = sample_scan.load_scanner(name)
        report = sample_scan.sample_scan(module, cpython, files)
        (out_dir / f"{name}.sample.json").write_text(
            json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
        )
        out[name] = report
        _log(f"  {name:26s} sample   {len(report.get('findings', [])):5d}")
    return out


def _type_counts(report: dict) -> str:
    counts: dict[str, int] = {}
    for f in report.get("findings", []):
        counts[f.get("type", "?")] = counts.get(f.get("type", "?"), 0) + 1
    if not counts:
        return "--"
    return ", ".join(
        f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
    )


def write_run_context(
    path: Path,
    sid: str,
    spec: dict,
    cpython: Path,
    corpus: str,
    baselines: dict[str, dict],
    samples: dict[str, dict],
    calibration: dict[str, list[str]],
    line_counts: dict[str, int],
    catalog_dir: Path | None,
    git_ref: str,
) -> None:
    new_territory = [f for f in spec["files"] if f not in calibration]
    lines: list[str] = []
    add = lines.append

    add(f"# Run context -- informed-explore, slice `{sid}`\n")
    add(f"**Slice:** {spec['family']} -- tier {spec['tier']}")
    add(f"**Target:** `{cpython}` @ `{git_ref}`")
    add(f"**Scope argument:** `{spec['scope']}`")
    add(f"**Size:** {len(spec['files'])} files, {spec['lines']:,} lines")
    if spec.get("oracle"):
        add(
            f"**Differential oracle:** `{spec['oracle']}` -- a shipped pure-Python twin. "
            "Feed the same adversarial input to both and compare exit codes; a C-side "
            "SIGSEGV/SIGABRT where the twin raises cleanly is a confirmed, localized bug."
        )
    if spec.get("passes", 1) > 1:
        add(
            f"\n> **This slice needs {spec['passes']} passes.** It is a single file above the "
            "13,000-line cap and cannot be split by file. Split it by subsystem -- see the "
            "slice notes -- and run one informed-explore per pass, each scoped to its "
            "subsystem by function, not by line range."
        )
    if spec.get("notes"):
        add(f"\n**Why this slice:** {spec['notes']}")

    add("\n## Purpose\n")
    add("Equal parts **bug hunt** and **toolkit feedback**. Two deliverables:\n")
    add(
        "1. **Findings** -- triaged FIX / CONSIDER / POLICY / ACCEPTABLE, reproduced where "
        "possible."
    )
    add(
        "2. **Toolkit assessment** -- precision per rule, recall gaps found by reading that the "
        "scanner missed (the most valuable output), and concrete tuning proposals.\n"
    )
    add(
        'A confident **negative** ("this class is clean here, and here is why") is a real '
        "result. Do not inflate.\n"
    )
    add(
        "**Check the denominator before reporting a clean negative.** A zero next to a zero "
        "denominator is silence, not safety -- four v0.8 rules produced structural zeros that "
        "were certified as clean. Every `*.sample.json` below was produced by re-running the "
        "scanner over exactly these files, so its denominators are slice-scoped and the canary "
        "works.\n"
    )

    add("## Files in this slice\n")
    if calibration:
        add(
            "**Calibration subset** -- these carry catalog entries, so they measure *recall*:\n"
        )
        add("| file | lines | prior entries |")
        add("|---|---|---|")
        for f in spec["files"]:
            if f in calibration:
                add(
                    f"| `{f}` | {line_counts.get(f, 0):,} | {', '.join(calibration[f])} |"
                )
        add("")
    if new_territory:
        add(
            "**New territory** -- no prior review, no catalog entries. Novel findings live "
            "here:\n"
        )
        add("| file | lines |")
        add("|---|---|")
        for f in new_territory:
            add(f"| `{f}` | {line_counts.get(f, 0):,} |")
        add("")
    if not calibration:
        add(
            "> Every file in this slice is new territory -- there is no recall measurement "
            "available. Weight the toolkit assessment toward *what you found by reading that "
            "the scanners missed*.\n"
        )

    add("## Pre-run scanner baseline (already executed)\n")
    add(
        f"Raw JSON in `<run>/scanners/`. `<scanner>.sample.json` is scoped to this slice "
        f"-- **read that one**. The `{corpus}/` column is the whole directory, there to answer "
        "one question: is this rule noisy *here*, or noisy everywhere? A rule that fires 19 "
        "times in the slice and 19 times in the tree is telling you something different from "
        "one that fires 19 times out of 400.\n"
    )
    add(f"| scanner | {corpus}/ | slice | slice finding types |")
    add("|---|---|---|---|")
    rows = sorted(
        SCANNERS,
        key=lambda n: -len(samples[n].get("findings", [])),
    )
    for name in rows:
        n_corpus = len(baselines[name].get("findings", []))
        n_sample = len(samples[name].get("findings", []))
        bold = "**" if n_sample else ""
        add(
            f"| `{name}` | {n_corpus} | {bold}{n_sample}{bold} | {_type_counts(samples[name])} |"
        )
    add("")

    add("## Informed mode\n")
    add(
        "`preflight/informed_briefing.md` is in this run directory. Every agent reads it "
        "first, then triages in informed mode:\n"
    )
    add(
        "1. **Confirm, don't re-litigate** a previously-recorded finding -- one line, move on."
    )
    add(
        "2. **Skip the false-positive classes** in the taxonomy; flagging one anyway requires "
        "justifying why *this* instance is not that class."
    )
    add(
        "3. **Hunt siblings**: for each finding, name its **guarded twin** (the correctly-handled "
        "sibling = the fix) and search for the other sites that lack it.\n"
    )

    add("## Environment\n")
    add("```")
    add("source ~/venvs/cpython-review-toolkit/bin/activate")
    add("```")
    add(
        "Builds: `~/projects/python_build_matrix/builds/<name>/python` -- "
        "`debug-gil-nojit`, `debug-ft-nojit`, `release-gil-nojit`, plus `-asan` / `-tsan` "
        "variants. **Diff any file you cite** between the matrix build's commit and the "
        "target ref, and say whether they are identical.\n"
    )
    add("Traps, all previously measured:\n")
    add(
        "- Ambient `python` is RustPython -- silently produces empty output. Name an "
        "explicit interpreter."
    )
    add(
        "- Debug builds can *hide* bugs: CPY-0049's disclosure only reaches its site on "
        "release. Check crash clusters on both."
    )
    add(
        "- `gh search issues` silently returns nothing here. Use "
        "`gh api -X GET search/issues -f q='repo:python/cpython <terms>'`."
    )
    add(
        "- Heredoc-to-stdin mangles multi-line Python. Write a `.py` file and run it.\n"
    )

    add("## Recording\n")
    if catalog_dir:
        add(
            f"Findings repo: `{catalog_dir}` -- one directory per finding under `reports/`, "
            "`meta.json` is the only source of truth (`INDEX.md` and `catalog/known_bugs.tsv` "
            "are generated). Check for prior art with the `gh api` form above **before** "
            "calling anything novel.\n"
        )
    else:
        add(
            "No `--catalog-dir` was given, so the briefing was built without the findings "
            "catalog and this run is not cumulative. Re-run with it to seed known findings.\n"
        )
    add(
        f"When the slice is finished, set `slices.{sid}.status` to `done` in "
        "`plugins/cpython-review-toolkit/data/review_slices.json` and commit it -- that is the "
        "campaign's cursor."
    )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=_DOC.splitlines()[0] if _DOC else None)
    ap.add_argument("slice_id")
    ap.add_argument("--cpython", type=Path, required=True, help="CPython checkout")
    ap.add_argument("--manifest", type=Path, default=_DATA / "review_slices.json")
    ap.add_argument("--reports-dir", type=Path, default=_REPO / "reports")
    ap.add_argument("--catalog-dir", type=Path, help="cpython-review-findings checkout")
    ap.add_argument(
        "--refresh-baseline",
        action="store_true",
        help="re-scan the surrounding directory instead of using the cache",
    )
    ap.add_argument("--quiet", action="store_true", help="suppress progress output")
    args = ap.parse_args(argv)

    global _QUIET
    _QUIET = args.quiet

    with open(args.manifest, encoding="utf-8") as fh:
        manifest = json.load(fh)
    if args.slice_id not in manifest["slices"]:
        raise SystemExit(
            f"unknown slice {args.slice_id!r}; try: python tools/slice_status.py"
        )
    spec = manifest["slices"][args.slice_id]
    cpython = args.cpython.resolve()
    if not (cpython / "Objects" / "object.c").is_file():
        raise SystemExit(f"{cpython} does not look like a CPython checkout")

    missing = [f for f in spec["files"] if not (cpython / f).is_file()]
    if missing:
        raise SystemExit(
            "these slice files do not exist in the checkout -- the manifest has drifted:\n  "
            + "\n  ".join(missing)
            + "\nrun: python tools/slice_status.py --verify "
            + str(cpython)
        )

    run_dir = args.reports_dir / args.slice_id
    (run_dir / "preflight").mkdir(parents=True, exist_ok=True)
    (run_dir / "agents").mkdir(exist_ok=True)
    (run_dir / "repro").mkdir(exist_ok=True)

    sample_scan = _import(_REPO / "tools" / "sample_scan.py", "sample_scan")
    briefing_mod = _import(
        _SCRIPTS / "build_informed_briefing.py", "build_informed_briefing"
    )

    _log(f"slice {args.slice_id}: {len(spec['files'])} files, {spec['lines']:,} lines")

    briefing = briefing_mod.build_briefing(
        shapes_path=str(_DATA / "cpython_bug_shapes.json"),
        non_bugs_path=str(_DATA / "cpython_non_bugs.md"),
        catalog_dir=str(args.catalog_dir.resolve()) if args.catalog_dir else None,
    )
    (run_dir / "preflight" / "informed_briefing.md").write_text(
        briefing, encoding="utf-8"
    )
    _log(
        f"  briefing {len(briefing.splitlines())} lines"
        f"{' (catalog folded in)' if args.catalog_dir else ''}"
    )

    corpus = corpus_of(spec)
    baselines = run_baselines(
        sample_scan,
        cpython / corpus,
        corpus,
        args.reports_dir / "_baseline",
        args.refresh_baseline,
    )
    samples = run_samples(sample_scan, cpython, spec["files"], run_dir / "scanners")

    known = known_bug_files(_DATA / "cpython_known_bugs.tsv")
    cataloged = catalog_files(args.catalog_dir.resolve() if args.catalog_dir else None)
    calibration: dict[str, list[str]] = {}
    for f in spec["files"]:
        ids = sorted(set(known.get(f, [])) | set(cataloged.get(f, [])))
        if ids:
            calibration[f] = ids

    line_counts: dict[str, int] = {}
    for f in spec["files"]:
        with open(cpython / f, "rb") as fh:
            line_counts[f] = sum(1 for _ in fh)

    try:
        import subprocess

        git_ref = (
            subprocess.run(
                ["git", "-C", str(cpython), "rev-parse", "--short", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            or "unknown"
        )
    except Exception:  # noqa: BLE001 -- a missing ref must not fail the setup
        git_ref = "unknown"

    write_run_context(
        run_dir / "preflight" / "RUN_CONTEXT.md",
        args.slice_id,
        spec,
        cpython,
        corpus,
        baselines,
        samples,
        calibration,
        line_counts,
        args.catalog_dir.resolve() if args.catalog_dir else None,
        git_ref,
    )

    total = sum(len(r.get("findings", [])) for r in samples.values())
    _log(f"\nrun dir: {run_dir}")
    _log(f"  {total} slice-scoped candidates across {len(SCANNERS)} scanners")
    _log(
        f"  {len(calibration)} calibration file(s), "
        f"{len(spec['files']) - len(calibration)} new territory"
    )
    _log(f"\nnext: /cpython-review-toolkit:informed-explore {spec['scope']} all")
    return 0


if __name__ == "__main__":
    sys.exit(main())

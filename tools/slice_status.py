#!/usr/bin/env python3
"""Campaign cursor for the informed-explore review slices.

Why this exists
---------------

The review campaign spans 37 slices across ~358,000 lines, run one slice per
session over many sessions. Nothing about that survives in conversation
context, so the state has to live on disk and be re-derivable in one command at
the top of a fresh session: what is done, what is next, and what each slice has
actually yielded.

Three things this reports, all derived rather than asserted:

* **Progress** -- per tier, from the manifest's ``status`` field.
* **Yield** -- findings per slice, counted from the findings repo's
  ``reports/*/meta.json`` by matching each finding's cited files back to the
  slice that owns them. A slice marked done with zero findings is a real
  result; a slice marked *pending* with findings means a sweep reached into it
  and it deserves a proper pass.
* **Drift** -- ``--verify`` re-walks a CPython checkout and asserts the manifest
  still partitions it exactly. CPython gains and loses files; a manifest that
  silently stops covering the tree would report a completeness it no longer
  has, which is the same "silence read as safety" failure the scanners had.

Usage
-----

    python tools/slice_status.py                          # progress + next slice
    python tools/slice_status.py --catalog-dir ~/projects/cpython-review-findings
    python tools/slice_status.py --next                    # just the next slice id
    python tools/slice_status.py --verify ~/projects/cpython   # drift check
    python tools/slice_status.py --sync ~/projects/cpython     # refresh line counts
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DOC = __doc__ or ""
MANIFEST = _REPO / "plugins" / "cpython-review-toolkit" / "data" / "review_slices.json"

_TIER_LABEL = {
    "A": "adversarial-object reachable",
    "B": "library and system surface",
    "C": "platform-locked / low yield",
}


def load_manifest(path: Path = MANIFEST) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def owner_of(manifest: dict, rel_path: str) -> str | None:
    """Return the slice id owning a repo-relative CPython source path."""
    for sid, spec in manifest["slices"].items():
        if rel_path in spec["files"]:
            return sid
    return None


def catalog_yield(
    manifest: dict, catalog_dir: Path
) -> tuple[dict[str, set[str]], set[str]]:
    """Map slice id -> finding ids, plus the ids that cite no owned file."""
    per_slice: dict[str, set[str]] = {}
    unowned: set[str] = set()
    reports = catalog_dir / "reports"
    if not reports.is_dir():
        raise SystemExit(f"no reports/ under {catalog_dir}")
    for meta_path in sorted(reports.glob("*/meta.json")):
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        fid = meta.get("id", meta_path.parent.name)
        hit = False
        for site in meta.get("sites", []):
            sid = owner_of(manifest, site.get("path", ""))
            if sid:
                per_slice.setdefault(sid, set()).add(fid)
                hit = True
        if not hit:
            unowned.add(fid)
    return per_slice, unowned


def is_excluded(excluded: dict, rel: str) -> bool:
    """True if the manifest's exclusion vocabulary covers this path.

    Kept module-level rather than closed over inside verify() so the invariant
    that matters -- no slice owns a file its own exclusion rules would drop --
    can be asserted directly instead of inferred from a tree walk.
    """
    if any(rel.startswith(p) for p in excluded.get("prefixes", ())):
        return True
    parts = rel.split("/")
    for seg in parts[:-1]:
        if seg in excluded.get("dir_names", ()):
            return True
        if any(seg.startswith(p) for p in excluded.get("dir_prefixes", ())):
            return True
    base = parts[-1]
    if base in excluded.get("names", ()):
        return True
    return any(base.startswith(p) for p in excluded.get("name_prefixes", ()))


def verify(manifest: dict, cpython_root: Path) -> list[str]:
    """Assert the manifest still partitions the live tree. Returns problems."""
    excluded = manifest["_meta"]["excluded"]
    present: set[str] = set()
    for top in ("Objects", "Modules"):
        base = cpython_root / top
        if not base.is_dir():
            return [f"{base} does not exist -- is {cpython_root} a CPython checkout?"]
        for dirpath, _, filenames in os.walk(base):
            for fn in filenames:
                if not fn.endswith(".c"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), cpython_root)
                if not is_excluded(excluded, rel):
                    present.add(rel)

    assigned: dict[str, str] = {}
    problems: list[str] = []
    for sid, spec in manifest["slices"].items():
        for f in spec["files"]:
            if f in assigned:
                problems.append(f"double-assigned {f}: {assigned[f]} / {sid}")
            assigned[f] = sid
    for f in sorted(present - set(assigned)):
        problems.append(f"NEW, unassigned: {f} -- add it to a slice")
    for f in sorted(set(assigned) - present):
        problems.append(f"GONE from the tree: {f} (slice {assigned[f]})")
    return problems


def sync_lines(manifest: dict, cpython_root: Path) -> list[str]:
    """Recompute per-slice and total line counts from the tree. Returns changes.

    Line counts drift every time CPython edits a file, and the manifest asserts
    its own totals in the test suite. Without this the only way to fix a stale
    count is by hand across 37 slices, which is how a manifest ends up quietly
    wrong.
    """
    changes: list[str] = []
    total_lines = 0
    total_files = 0
    for sid, spec in manifest["slices"].items():
        before = spec["lines"]
        after = 0
        for rel in spec["files"]:
            path = cpython_root / rel
            if not path.is_file():
                changes.append(f"{sid}: {rel} is missing -- run --verify")
                continue
            with open(path, "rb") as fh:
                after += sum(1 for _ in fh)
        if after != before:
            changes.append(f"{sid}: {before:,} -> {after:,} lines")
        spec["lines"] = after
        total_lines += after
        total_files += len(spec["files"])
    if manifest["_meta"]["total_lines"] != total_lines:
        changes.append(
            f"total: {manifest['_meta']['total_lines']:,} -> {total_lines:,} lines"
        )
    manifest["_meta"]["total_lines"] = total_lines
    manifest["_meta"]["total_files"] = total_files
    return changes


def next_slice(manifest: dict) -> str | None:
    slices = manifest["slices"]
    for sid in manifest["_meta"]["order"]:
        if slices.get(sid, {}).get("status") == "pending":
            return sid
    return None


def report(manifest: dict, per_slice: dict[str, set[str]] | None) -> None:
    slices = manifest["slices"]
    order = manifest["_meta"]["order"]
    # Completed historical slices are not in `order`; show them first.
    historical = [s for s in slices if s not in order]

    total_lines = manifest["_meta"]["total_lines"]
    done_lines = sum(s["lines"] for s in slices.values() if s["status"] == "done")
    print(
        f"campaign: {len(slices)} slices, {manifest['_meta']['total_files']} files, "
        f"{total_lines:,} lines"
    )
    print(
        f"complete: {sum(1 for s in slices.values() if s['status'] == 'done')} slices, "
        f"{done_lines:,} lines ({done_lines * 100 // total_lines}%)\n"
    )

    for tier in ("A", "B", "C"):
        ids = [s for s in historical + order if slices[s]["tier"] == tier]
        if not ids:
            continue
        tier_done = sum(1 for s in ids if slices[s]["status"] == "done")
        print(f"--- TIER {tier} ({_TIER_LABEL[tier]}): {tier_done}/{len(ids)} done")
        for sid in ids:
            spec = slices[sid]
            mark = {"done": "[x]", "in-progress": "[~]", "pending": "[ ]"}.get(
                spec["status"], "[?]"
            )
            found = ""
            if per_slice is not None:
                n = len(per_slice.get(sid, ()))
                if n:
                    flag = (
                        "" if spec["status"] == "done" else "  <- swept, not reviewed"
                    )
                    found = f"  {n} finding{'s' if n != 1 else ''}{flag}"
            passes = f" x{spec['passes']}" if spec.get("passes", 1) > 1 else ""
            print(
                f"  {mark} {sid:24s} {len(spec['files']):2d}f "
                f"{spec['lines']:7,d}l{passes:3s}{found}"
            )
        print()

    nxt = next_slice(manifest)
    if nxt:
        spec = slices[nxt]
        print(f"NEXT: {nxt} -- {spec['family']}")
        print(
            f"  scope   {spec['scope']}   ({len(spec['files'])} files, {spec['lines']:,} lines)"
        )
        if spec.get("oracle"):
            print(f"  oracle  {spec['oracle']}")
        if spec.get("passes", 1) > 1:
            print(
                f"  passes  {spec['passes']} -- too large for one pass, split by subsystem"
            )
        if spec.get("notes"):
            print(f"  notes   {spec['notes']}")
        print(f"\n  python tools/make_slice_context.py {nxt} --cpython <root>")
    else:
        print("NEXT: nothing pending -- campaign complete.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=_DOC.splitlines()[0] if _DOC else None)
    ap.add_argument("--manifest", type=Path, default=MANIFEST)
    ap.add_argument("--catalog-dir", type=Path, help="cpython-review-findings checkout")
    ap.add_argument(
        "--verify",
        type=Path,
        metavar="CPYTHON_ROOT",
        help="check the manifest still partitions the tree",
    )
    ap.add_argument(
        "--sync",
        type=Path,
        metavar="CPYTHON_ROOT",
        help="recompute line counts from the tree and rewrite the manifest",
    )
    ap.add_argument(
        "--next", action="store_true", help="print the next slice id and exit"
    )
    args = ap.parse_args(argv)

    manifest = load_manifest(args.manifest)

    if args.next:
        nxt = next_slice(manifest)
        if not nxt:
            return 1
        print(nxt)
        return 0

    if args.sync:
        changes = sync_lines(manifest, args.sync.resolve())
        with open(args.manifest, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")
        if not changes:
            print("OK: line counts already match the tree.")
            return 0
        for c in changes:
            print(c)
        print(f"\nrewrote {args.manifest}")
        return 0

    if args.verify:
        problems = verify(manifest, args.verify.resolve())
        if problems:
            for p in problems:
                print("DRIFT:", p)
            print(
                f"\n{len(problems)} problem(s) -- the manifest no longer covers the tree."
            )
            return 1
        print(
            f"OK: manifest partitions {args.verify} exactly "
            f"({manifest['_meta']['total_files']} files)."
        )
        return 0

    per_slice = None
    if args.catalog_dir:
        per_slice, unowned = catalog_yield(manifest, args.catalog_dir.resolve())
        report(manifest, per_slice)
        if unowned:
            print(
                f"\n{len(unowned)} finding(s) cite no file this manifest owns "
                f"(Python/ or Include/): {', '.join(sorted(unowned))}"
            )
        return 0

    report(manifest, None)
    return 0


if __name__ == "__main__":
    sys.exit(main())

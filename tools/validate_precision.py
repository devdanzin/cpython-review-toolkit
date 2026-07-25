#!/usr/bin/env python3
"""Mechanical precision / line-accuracy harness for cpython-review-toolkit scanners.

Dev tooling, not part of the shipped plugin: it runs every scanner over real
CPython source and measures the two things a human cannot eyeball at scale.

1. **Volume** per scanner and per finding ``type`` — so a rule that floods, or a
   rule that has silently stopped firing, is visible immediately.
2. **Line accuracy** — the check that matters most. Several scanners strip
   comments with a substitution that deletes newlines, which shifts every
   reported line. For each finding we look at the line the scanner reported in
   the *real* file and ask whether the construct the finding is about is
   actually there (within a small window). A scanner can be perfectly precise
   about *what* is wrong and still be useless if it points at the wrong line.

Usage::

    python tools/validate_precision.py --cpython ~/projects/cpython            # measure
    python tools/validate_precision.py --cpython ~/projects/cpython \
        --save baseline.json                                                    # record
    python tools/validate_precision.py --cpython ~/projects/cpython \
        --baseline baseline.json                                                # compare

``--scanners a,b,c`` limits the run; ``--dirs Objects,Modules`` limits targets.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "cpython-review-toolkit"
SCRIPTS = PLUGIN / "scripts"

# Scanners that take (target, --max-files) and emit the standard envelope.
SCANNERS = [
    "scan_recursion_guards",
    "scan_pyerr_clear",
    "scan_uninit_dealloc",
    "scan_init_bypass",
    "scan_ft_races",
    "scan_stw_safety",
    "scan_lock_discipline",
    "scan_refcounts",
    "scan_error_paths",
    "scan_null_checks",
    "scan_memory_patterns",
    "scan_gil_usage",
]

DEFAULT_DIRS = ["Objects", "Modules", "Python"]

# A finding's own `detail` almost always names the construct it is about — a
# variable, a field, or an API — in quotes or as a C identifier. Checking the
# reported line against *that* is scanner-agnostic and self-describing, and it
# avoids the trap of hard-coding what we *assume* a rule points at.
#
# That trap is real: an early version of this harness expected
# `dealloc_of_uninitialized_object` to sit on the `Py_DECREF` line and scored the
# scanner 0%, when in fact the rule correctly points at the *allocation* — the
# line a reviewer must change. The expectation was wrong, not the scanner.
#
# TYPE_TOKENS is now only a fallback for findings whose detail names nothing
# checkable, and deliberately errs toward "no expectation" (excluded from the
# denominator) rather than a guessed one.
TYPE_TOKENS: dict[str, tuple[str, ...]] = {
    "pyerr_clear": ("PyErr_Clear", "_PyErr_Clear"),
    "untrack": ("GC_UNTRACK", "GC_UnTrack", "GC_Track", "GC_TRACK"),
    "stw": ("StopTheWorld", "StartTheWorld"),
}
WINDOW = 1  # lines either side that still count as "lands on the construct"

# Identifiers too generic to prove anything if they happen to appear on a line.
_NOISE = frozenset({
    "the", "a", "an", "is", "of", "to", "in", "on", "at", "and", "or", "not",
    "may", "can", "with", "before", "after", "line", "NULL", "if", "return",
    "function", "call", "may_release", "value", "object", "member", "field",
    "int", "char", "void", "static", "const", "self", "type", "size",
})


def _identifiers_from_detail(detail: str, enclosing_fn: str = "") -> list[str]:
    """Pull checkable identifiers out of a finding's prose detail.

    Takes the union of quoted tokens ('seq', `tuple_args`) *and* bare C symbols
    (`PyObject_Hash`, `select`), because scanners split those two roles
    inconsistently — some quote the API, some quote only the enclosing function
    and leave the API bare.

    **Excludes the enclosing function's own name.** That was the harness's
    second wrong turn: details routinely open with the function under analysis
    ("'method_hash' calls PyObject_Hash on…"), and a function's name does not
    appear on the line where its defect lives — so keying on it scored correct
    scanners at ~2-4%. The defect line holds the *called* symbol, not the caller.
    """
    if not detail:
        return []
    # Quoted tokens may be member expressions ('descr->d_qualname', 'x.field') —
    # match the whole thing, then also contribute each component, since the
    # source line usually shows the field rather than the full expression.
    quoted_raw = re.findall(r"[`'\"]([A-Za-z_][A-Za-z0-9_.>\-]*)[`'\"]", detail)
    quoted: list[str] = []
    for q in quoted_raw:
        quoted.append(q)
        quoted.extend(p for p in re.split(r"->|\.", q) if p)
    # Bare identifiers only when they are being *called* — "PyObject_Hash(" —
    # which keeps English prose ("write", "macro") out of the token set.
    bare = re.findall(
        r"\b((?:Py|_Py)[A-Za-z0-9_]+|[a-z][a-z0-9]*_[a-z0-9_]+|[a-z]{3,})\s*\(", detail
    )
    out: list[str] = []
    for tok in [*quoted, *bare]:
        if tok in _NOISE or len(tok) <= 2 or tok == enclosing_fn:
            continue
        if tok not in out:
            out.append(tok)
    return out


def run_scanner(script: str, target: Path, cpython: Path) -> dict | None:
    """Run one scanner over one directory; return its parsed report."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / f"{script}.py"), str(target)],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
        cwd=str(cpython),
        timeout=1800,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        return {"_error": (proc.stderr or "").strip()[:400]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"_error": f"unparseable JSON: {exc}"}


def iter_findings(report: dict):
    """Yield findings from either the standard envelope or check_pep7's shape."""
    yield from report.get("findings", []) or []
    for f in report.get("files", []) or []:
        for v in f.get("violations", []) or []:
            yield {**v, "file": f.get("file", ""), "type": v.get("rule", "")}


def expected_tokens(finding: dict) -> tuple[str, ...]:
    """What the reported line should contain, preferring the finding's own words."""
    enclosing = str(finding.get("function") or finding.get("enclosing_function") or "")
    idents = _identifiers_from_detail(finding.get("detail", "") or "", enclosing)
    if idents:
        return tuple(idents)
    ftype = (finding.get("type") or "").lower()
    for key in sorted(TYPE_TOKENS, key=len, reverse=True):
        if key in ftype:
            return TYPE_TOKENS[key]
    return ()


def check_line(cpython: Path, finding: dict, cache: dict[str, list[str]]) -> str | None:
    """'hit' if the reported line carries the expected construct, else 'miss'.

    Returns None when we have no expectation for this finding (so it is excluded
    from the accuracy denominator rather than silently counted as a pass).
    """
    rel, line = finding.get("file", ""), finding.get("line", 0)
    if not rel or not isinstance(line, int) or line <= 0:
        return None
    toks = expected_tokens(finding)
    if not toks:
        return None
    if rel not in cache:
        p = cpython / rel
        if not p.is_file():
            cache[rel] = []
        else:
            cache[rel] = p.read_text(encoding="utf-8", errors="replace").splitlines()
    lines = cache[rel]
    if not lines:
        return None
    lo, hi = max(0, line - 1 - WINDOW), min(len(lines), line + WINDOW)
    window = "\n".join(lines[lo:hi])
    return "hit" if any(t in window for t in toks) else "miss"


def measure(cpython: Path, scanners: list[str], dirs: list[str]) -> dict:
    cache: dict[str, list[str]] = {}
    out: dict = {"cpython": str(cpython), "dirs": dirs, "scanners": {}}
    for script in scanners:
        entry: dict = {"total": 0, "by_type": {}, "line_hit": 0, "line_miss": 0,
                       "errors": [], "by_dir": {}}
        for d in dirs:
            target = cpython / d
            if not target.is_dir():
                continue
            rep = run_scanner(script, target, cpython)
            if rep is None or "_error" in (rep or {}):
                entry["errors"].append({d: (rep or {}).get("_error", "no output")})
                continue
            n = 0
            for f in iter_findings(rep):
                n += 1
                entry["total"] += 1
                t = f.get("type", "?") or "?"
                entry["by_type"][t] = entry["by_type"].get(t, 0) + 1
                verdict = check_line(cpython, f, cache)
                if verdict == "hit":
                    entry["line_hit"] += 1
                elif verdict == "miss":
                    entry["line_miss"] += 1
            entry["by_dir"][d] = n
        checked = entry["line_hit"] + entry["line_miss"]
        entry["line_accuracy"] = round(entry["line_hit"] / checked, 4) if checked else None
        entry["lines_checked"] = checked
        out["scanners"][script] = entry
    return out


def render(result: dict, baseline: dict | None) -> str:
    rows = []
    hdr = f"{'scanner':<24}{'findings':>9}{'line-acc':>10}{'checked':>9}"
    if baseline:
        hdr += f"{'Δ findings':>12}{'Δ line-acc':>12}"
    rows.append(hdr)
    rows.append("-" * len(hdr))
    for name, e in result["scanners"].items():
        acc = e["line_accuracy"]
        accs = f"{acc*100:.1f}%" if acc is not None else "n/a"
        row = f"{name:<24}{e['total']:>9}{accs:>10}{e['lines_checked']:>9}"
        if baseline:
            b = baseline.get("scanners", {}).get(name)
            if b:
                dv = e["total"] - b["total"]
                row += f"{dv:>+12}"
                ba = b.get("line_accuracy")
                if acc is not None and ba is not None:
                    row += f"{(acc-ba)*100:>+11.1f}%"
                else:
                    row += f"{'n/a':>12}"
            else:
                row += f"{'NEW':>12}{'':>12}"
        rows.append(row)
        if e["errors"]:
            rows.append(f"    !! errors: {e['errors']}")
    return "\n".join(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cpython", required=True, type=Path)
    ap.add_argument("--scanners", default="")
    ap.add_argument("--dirs", default=",".join(DEFAULT_DIRS))
    ap.add_argument("--save", type=Path)
    ap.add_argument("--baseline", type=Path)
    ap.add_argument("--json", action="store_true", help="dump the raw result")
    args = ap.parse_args()

    scanners = [s for s in (args.scanners.split(",") if args.scanners else SCANNERS) if s]
    dirs = [d for d in args.dirs.split(",") if d]
    result = measure(args.cpython.expanduser().resolve(), scanners, dirs)

    baseline = None
    if args.baseline and args.baseline.is_file():
        baseline = json.loads(args.baseline.read_text())

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render(result, baseline))

    if args.save:
        args.save.write_text(json.dumps(result, indent=2))
        print(f"\nsaved -> {args.save}", file=sys.stderr)


if __name__ == "__main__":
    main()

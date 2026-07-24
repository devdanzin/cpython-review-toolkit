#!/usr/bin/env python3
"""Assemble the informed-explore briefing from the CPython bug-shape catalogs.

This is a **generator**, not a scanner: it emits a **Markdown briefing** (to
stdout) that every agent in the ``informed-explore`` command reads first. It is
the deterministic, reusable form of the hand-built briefing that proved out in
the sibling toolkits' informed runs. It seeds each agent with

  (a) the bug-SHAPE catalog (``data/cpython_bug_shapes.json``) as sibling-hunt
      templates, each with its **guarded twin** (the correct sibling = the fix),
  (b) the cross-cutting triage rules (guarded-twin, both-crash, reachability,
      confirm-don't-relitigate), and
  (c) the false-positive taxonomy (``data/cpython_non_bugs.md``) to suppress.

The bug shapes are CPython-INDEPENDENT of any single module, so one briefing
applies to any scope of a CPython checkout. If a durable findings repo
(``cpython-review-findings``, the static analog of ``cpython-oom-findings`` /
``cpython-tsan-findings``) is checked out, pass ``--catalog-dir`` to fold its
``reports/*/meta.json`` entries in as "confirm, don't re-litigate" leads.

Output is Markdown, not JSON, because it is consumed directly as an agent
briefing::

    python build_informed_briefing.py > reports/preflight/informed_briefing.md
    python build_informed_briefing.py --catalog-dir ~/projects/cpython-review-findings

The CLI shape mirrors ``scan_common.parse_common_args`` (a small hand-rolled
flag loop, writes to stdout, exposes ``main()``), but there is no positional
target and no ``--max-files``: the briefing is target-independent.
"""

import json
import sys
from pathlib import Path

_DATA = Path(__file__).resolve().parent.parent / "data"
_DEFAULT_SHAPES = _DATA / "cpython_bug_shapes.json"
_DEFAULT_NON_BUGS = _DATA / "cpython_non_bugs.md"


# ---------------------------------------------------------------------------
# Catalog loaders
# ---------------------------------------------------------------------------


def _load_shapes(shapes_path: str | None = None) -> dict:
    """Load ``cpython_bug_shapes.json`` → ``{"shapes": [...], "triage_rules": [...]}``.

    A missing/invalid file yields empty lists (the briefing degrades gracefully
    rather than crashing an entire informed run).
    """
    path = Path(shapes_path) if shapes_path else _DEFAULT_SHAPES
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"shapes": [], "triage_rules": []}
    shapes = [s for s in data.get("shapes", []) if isinstance(s, dict) and s.get("id")]
    rules = [r for r in data.get("triage_rules", []) if isinstance(r, str)]
    return {"shapes": shapes, "triage_rules": rules}


def _load_non_bugs(non_bugs_path: str | None = None) -> str:
    """Return the FP-taxonomy markdown (``data/cpython_non_bugs.md``)."""
    path = Path(non_bugs_path) if non_bugs_path else _DEFAULT_NON_BUGS
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _first_site(meta: dict) -> str:
    """Best-effort single-site string from a findings-repo ``meta.json``.

    Tolerates the two documented layouts (``sites`` list / ``signature``) plus
    the ``signatures`` list some repos use, and a dict-shaped ``signature``.
    """
    sites = meta.get("sites")
    if isinstance(sites, list) and sites:
        return str(sites[0])
    sigs = meta.get("signatures")
    if isinstance(sigs, list) and sigs:
        return str(sigs[0])
    sig = meta.get("signature")
    if isinstance(sig, dict):
        return str(sig.get("site_frame") or next(iter(sig.values()), "-"))
    if isinstance(sig, str) and sig:
        return sig
    return "-"


def _load_catalog(catalog_dir: str) -> list[dict]:
    """Fold in recorded findings from a ``cpython-review-findings`` repo.

    Reads ``<catalog_dir>/reports/*/meta.json`` (the findings-repo layout) and
    maps each entry to the briefing shape (id / category / site / title /
    status). A missing/empty dir yields an empty list so the caller omits the
    section entirely.
    """
    out: list[dict] = []
    for meta_path in sorted(Path(catalog_dir).glob("reports/*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not meta.get("id"):
            continue
        out.append(
            {
                "id": meta["id"],
                "category": meta.get("category", "-"),
                "site": _first_site(meta),
                "title": meta.get("title", meta.get("slug", "")),
                "status": meta.get("status", "confirmed"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Markdown assembly
# ---------------------------------------------------------------------------

_INFORMED_RULES = (
    "1. **Confirm, don't re-litigate.** If a candidate matches a "
    "previously-recorded finding (the catalog section below, if present), tally "
    "it in one line and move on — do not re-describe a known bug.\n"
    "2. **Skip the known false-positive classes** (the taxonomy below). If you "
    "flag something that falls in one of those classes, you must say *why this "
    "instance is NOT that FP class*.\n"
    "3. **Hunt siblings via the guarded twin.** For each bug SHAPE relevant to "
    "your scope, locate its **guarded twin** (the correctly-handled sibling in "
    "the same file/family = the fix), then search for the other sites that lack "
    "it — those un-found siblings are the point of an informed run."
)


def _escape_cell(text: str) -> str:
    return str(text).replace("|", r"\|").replace("\n", " ")


def _render_shape(shape: dict) -> str:
    lines = [
        f"### {shape['id']} — {shape.get('title', '')}",
        f"- **severity (default before triage):** {shape.get('severity', 'CONSIDER')}",
        f"- **pattern:** {shape.get('pattern', '')}",
        f"- **guarded twin (the fix):** {shape.get('guarded_twin', '')}",
        f"- **hunt:** {shape.get('hunt', '')}",
    ]
    if shape.get("differential"):
        lines.append(f"- **differential (how to confirm):** {shape['differential']}")
    examples = shape.get("confirmed_examples") or []
    if examples:
        lines.append(f"- **confirmed examples:** {', '.join(examples)}")
    if shape.get("scanner"):
        lines.append(f"- **surfaced by:** `{shape['scanner']}`")
    return "\n".join(lines)


def build_briefing(
    *,
    shapes_path: str | None = None,
    non_bugs_path: str | None = None,
    catalog_dir: str | None = None,
) -> str:
    """Assemble the informed-explore briefing markdown (deterministic).

    ``shapes_path`` / ``non_bugs_path`` override the default ``data/`` catalogs
    (used by tests). ``catalog_dir`` optionally points at a
    ``cpython-review-findings`` repo whose ``reports/*/meta.json`` entries are
    folded in as recorded findings.
    """
    data = _load_shapes(shapes_path)
    shapes = data["shapes"]
    triage_rules = data["triage_rules"]
    non_bugs_md = _load_non_bugs(non_bugs_path)
    findings = _load_catalog(catalog_dir) if catalog_dir else []

    out: list[str] = []
    out.append("# Informed-exploration briefing — CPython C code review\n")
    out.append(
        "You are running as part of an **informed** explore. Unlike a cold run, "
        "you have the catalog of recurring CPython C bug SHAPES (reusable "
        "templates — not file:line, since the scope differs every run), the "
        "cross-cutting triage rules, and the false-positive taxonomy. Use them: "
        "the goal is fix-propagation (find *every* instance of a known shape) "
        "and genuinely new territory, not re-discovering the catalog.\n"
    )

    out.append("## Your three informed-mode rules\n")
    out.append(_INFORMED_RULES + "\n")

    out.append("## Cross-cutting triage rules (apply to every finding)\n")
    if triage_rules:
        out.append("\n".join(f"- {rule}" for rule in triage_rules) + "\n")
    else:
        out.append("_(no triage rules loaded — see data/cpython_bug_shapes.json)_\n")

    out.append("## Bug-shape catalog — sibling-hunt templates\n")
    if shapes:
        out.append(
            f"{len(shapes)} recurring shapes. For each, the **guarded twin** is "
            "the correctly-handled sibling that both confirms the finding and "
            "*is* the fix.\n"
        )
        for shape in shapes:
            out.append(_render_shape(shape) + "\n")
    else:
        out.append("_(no shapes loaded — see data/cpython_bug_shapes.json)_\n")

    if findings:
        out.append(
            "## Previously-recorded findings (confirm, don't re-litigate; hunt siblings)\n"
        )
        out.append(
            f"From the `cpython-review-findings` catalog ({len(findings)} "
            "recorded). These are context, not fresh discoveries: confirm each "
            "still reproduces in one line, then spend your effort on **un-found "
            "siblings** of the same shape (via its guarded twin).\n"
        )
        out.append("| id | category | site | title | status |")
        out.append("|----|----------|------|-------|--------|")
        for f in findings:
            out.append(
                f"| {_escape_cell(f['id'])} "
                f"| {_escape_cell(f['category'])} "
                f"| `{_escape_cell(f['site'])}` "
                f"| {_escape_cell(f['title'])} "
                f"| {_escape_cell(f['status'])} |"
            )
        out.append("")

    out.append(
        "## Known false-positive classes — DO NOT re-report (justify if you flag one)\n"
    )
    if non_bugs_md:
        out.append(
            "The full taxonomy lives in `data/cpython_non_bugs.md`; it is "
            "reproduced here so every agent sees it inline.\n"
        )
        out.append(non_bugs_md)
    else:
        out.append("_(taxonomy file missing — see data/cpython_non_bugs.md)_")

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> tuple[str | None, str | None, str | None]:
    """Parse ``[--catalog-dir DIR] [--shapes PATH] [--non-bugs PATH]``.

    Modeled on ``scan_common.parse_common_args`` (a small flag loop), but with
    no positional target: the briefing is target-independent.
    """
    catalog_dir: str | None = None
    shapes_path: str | None = None
    non_bugs_path: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--catalog-dir" and i + 1 < len(argv):
            catalog_dir = argv[i + 1]
            i += 2
        elif arg == "--shapes" and i + 1 < len(argv):
            shapes_path = argv[i + 1]
            i += 2
        elif arg == "--non-bugs" and i + 1 < len(argv):
            non_bugs_path = argv[i + 1]
            i += 2
        else:
            i += 1
    return catalog_dir, shapes_path, non_bugs_path


def main() -> None:
    try:
        catalog_dir, shapes_path, non_bugs_path = _parse_args(sys.argv[1:])
        briefing = build_briefing(
            shapes_path=shapes_path,
            non_bugs_path=non_bugs_path,
            catalog_dir=catalog_dir,
        )
        # Markdown, not JSON: the output IS the agent briefing (see docstring).
        sys.stdout.write(briefing)
    except Exception as e:  # noqa: BLE001 -- top-level guard
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()

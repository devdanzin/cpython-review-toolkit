"""Task (e): sweep for the CPY-0187 shape tree-wide.

CPY-0187 = `bytearray.__init__` is the one clinic entry point of its type that
takes no critical section while 33 siblings do, and it writes self's fields
directly.  The signal is the RATIO, not any single site -- which is Group B's
`clinic_critical_section_coverage` proposal.  This measures it over every
Argument-Clinic-using .c file in CPython, so the proposal can be judged on
precision tree-wide rather than on the two types in one slice.

For each <file>.c with a sibling clinic/<file>.c.h:
  * parse the clinic header for `<impl>(` call sites and whether the wrapper
    holds Py_BEGIN_CRITICAL_SECTION* at that call;
  * an impl is GUARDED if the wrapper locks it, or the impl body opens its own
    critical section, or it is named *_lock_held;
  * an UNGUARDED impl is reported only when it *directly* writes a field of its
    first parameter (`self->F = ...`, `self->F++`, `Py_SET_SIZE(self, ...)`),
    i.e. not when it merely delegates to a locking helper.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPTS = "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts"
sys.path.insert(0, SCRIPTS)

import scan_ft_races as sfr  # noqa: E402
import tree_sitter_utils as tsu  # noqa: E402

CPY = Path("/home/danzin/projects/cpython")

CALL_RE = re.compile(r"\b(\w+_impl)\s*\(")
CS_BEGIN = re.compile(r"\bPy_BEGIN_CRITICAL_SECTION2?\s*\(")
CS_END = re.compile(r"\bPy_END_CRITICAL_SECTION2?\s*\(")


def clinic_lock_map(header: str) -> tuple[set[str], set[str]]:
    """(impls called under a wrapper CS, all impls called from the header)."""
    src = sfr.strip_comments(header)
    spans = sfr._critical_section_spans(src)
    locked, seen = set(), set()
    for m in CALL_RE.finditer(src):
        seen.add(m.group(1))
        if any(s <= m.start() < e for s, e in spans):
            locked.add(m.group(1))
    return locked, seen


FIELD_WRITE = re.compile(
    r"\b{v}\s*->\s*(\w+)\s*(?:\+\+|--|(?<![=!<>+\-*/%&|^])=(?!=))"
)
SETSIZE = re.compile(r"\bPy_SET_SIZE\s*\(\s*(?:\([^)]*\)\s*)?{v}\b")


def first_param(func: dict) -> str | None:
    params = (func.get("parameters") or "").strip("()")
    if not params:
        return None
    first = params.split(",")[0].strip()
    m = re.search(r"(\w+)\s*$", first)
    return m.group(1) if m else None


def main() -> None:
    rows = []
    for root in ("Objects", "Modules", "Python"):
        for hdr in sorted((CPY / root).rglob("clinic/*.c.h")):
            cfile = hdr.parent.parent / hdr.name[:-2]
            if not cfile.exists():
                continue
            locked, seen = clinic_lock_map(hdr.read_text(errors="replace"))
            if not locked:
                continue  # type has no @critical_section at all -> not the shape
            b = cfile.read_bytes()
            try:
                tree = tsu.parse_bytes(b)
            except Exception:
                continue
            funcs = {f["name"]: f for f in tsu.extract_functions(tree, b)}
            unguarded_writers = []
            for name in sorted(seen - locked):
                f = funcs.get(name)
                if f is None:
                    continue
                body = sfr.strip_comments(f["body"])
                if sfr._has_lock(body) or sfr._caller_holds_lock(name):
                    continue
                v = first_param(f)
                if not v:
                    continue
                fw = re.compile(FIELD_WRITE.pattern.replace("{v}", re.escape(v)))
                ss = re.compile(SETSIZE.pattern.replace("{v}", re.escape(v)))
                hits = sorted({m.group(1) for m in fw.finditer(body)})
                nsize = len(ss.findall(body))
                if hits or nsize:
                    unguarded_writers.append(
                        (name, f["start_line"], hits, nsize)
                    )
            rows.append(
                (
                    str(cfile.relative_to(CPY)),
                    len(locked),
                    len(seen),
                    unguarded_writers,
                )
            )

    rows.sort(key=lambda r: -len(r[3]))
    tot_files = len(rows)
    tot_find = sum(len(r[3]) for r in rows)
    print(f"files with >=1 @critical_section clinic method: {tot_files}")
    print(f"gated findings (unguarded impl that directly writes self's fields): {tot_find}\n")
    for rel, nlocked, nseen, writers in rows:
        if not writers:
            continue
        print(f"{rel}   {nlocked}/{nseen} clinic impls locked")
        for name, line, fields, nsize in writers:
            print(f"     {name}:{line}  writes={fields} Py_SET_SIZE x{nsize}")
    print("\n--- files with full coverage (no finding) ---")
    for rel, nlocked, nseen, writers in rows:
        if not writers:
            print(f"    {rel}  {nlocked}/{nseen}")


if __name__ == "__main__":
    main()

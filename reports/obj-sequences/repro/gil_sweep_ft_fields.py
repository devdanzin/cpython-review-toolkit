"""Enumerate every access to the mutable state of list/bytes/bytearray and
classify each by the synchronisation available at that site.

Output: one row per (file, function, field, line, access-kind, sync-class).

sync-class:
  CS_SELF      function body contains Py_BEGIN_CRITICAL_SECTION or the
               _Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED assertion
  CS_CLINIC    the clinic wrapper for this *_impl takes Py_BEGIN_CRITICAL_SECTION
  ATOMIC       the access text uses FT_ATOMIC_* / _Py_atomic_*
  PLAIN        neither
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CPY = Path("/home/danzin/projects/cpython")

FILES = [
    "Objects/listobject.c",
    "Objects/bytesobject.c",
    "Objects/bytearrayobject.c",
    "Objects/bytes_methods.c",
]

CLINIC = {
    "Objects/listobject.c": "Objects/clinic/listobject.c.h",
    "Objects/bytesobject.c": "Objects/clinic/bytesobject.c.h",
    "Objects/bytearrayobject.c": "Objects/clinic/bytearrayobject.c.h",
}

# The mutable state of the three types.
FIELDS = [
    "ob_item",
    "allocated",
    "ob_start",
    "ob_bytes",
    "ob_alloc",
    "ob_exports",
    "ob_bytes_object",
    "ob_sval",
    "ob_shash",
    "it_index",
    "it_seq",
]

FUNC_START = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def clinic_cs_impls(path: Path) -> set[str]:
    """Names of *_impl functions the clinic wrapper calls inside a CS."""
    if not path.exists():
        return set()
    lines = path.read_text(errors="replace").splitlines()
    out: set[str] = set()
    depth_open = False
    for i, ln in enumerate(lines):
        if "Py_BEGIN_CRITICAL_SECTION" in ln:
            depth_open = True
            continue
        if depth_open:
            m = re.search(r"([A-Za-z_][A-Za-z0-9_]*_impl)\s*\(", ln)
            if m:
                out.add(m.group(1))
            if "Py_END_CRITICAL_SECTION" in ln:
                depth_open = False
    return out


def functions(src: list[str]) -> list[tuple[str, int, int]]:
    """(name, start_line_1based, end_line_1based) for column-0 definitions."""
    out = []
    cur = None
    for i, ln in enumerate(src):
        if ln.startswith("}") and cur is not None:
            out.append((cur[0], cur[1], i + 1))
            cur = None
            continue
        m = FUNC_START.match(ln)
        if m and cur is None and (ln.rstrip().endswith("{") or _next_brace(src, i)):
            cur = (m.group(1), i + 1)
    return out


def _next_brace(src: list[str], i: int) -> bool:
    for j in range(i + 1, min(i + 6, len(src))):
        s = src[j].strip()
        if s.startswith("{"):
            return True
        if s.startswith("/*[clinic") or s.endswith(")") or s.endswith(","):
            continue
        if s == "":
            continue
        return False
    return False


def main() -> None:
    rows = []
    for rel in FILES:
        path = CPY / rel
        src = path.read_text(errors="replace").splitlines()
        cs_impls = clinic_cs_impls(CPY / CLINIC[rel]) if rel in CLINIC else set()
        funcs = functions(src)
        for name, start, end in funcs:
            body = "\n".join(src[start - 1 : end])
            has_cs = (
                "Py_BEGIN_CRITICAL_SECTION" in body
                or "_Py_CRITICAL_SECTION_ASSERT" in body
            )
            clinic_cs = name in cs_impls
            for off, ln in enumerate(src[start - 1 : end]):
                stripped = ln.split("/*")[0].split("//")[0]
                for f in FIELDS:
                    if not re.search(r"\b" + f + r"\b", stripped):
                        continue
                    atomic = (
                        "FT_ATOMIC" in stripped
                        or "_Py_atomic" in stripped
                        or "PyList_GET_SIZE" in stripped
                        or "PyByteArray_GET_SIZE" in stripped
                    )
                    # crude write detection
                    write = bool(
                        re.search(
                            r"\b" + f + r"\b\s*(=[^=]|\+\+|--|\+=|-=)", stripped
                        )
                    ) or bool(re.search(r"(\+\+|--)\s*[\w>.\-]*\b" + f + r"\b", stripped))
                    sync = (
                        "ATOMIC"
                        if atomic
                        else "CS_SELF"
                        if has_cs
                        else "CS_CLINIC"
                        if clinic_cs
                        else "PLAIN"
                    )
                    rows.append(
                        (
                            rel,
                            name,
                            f,
                            start + off,
                            "W" if write else "R",
                            sync,
                            stripped.strip()[:90],
                        )
                    )

    want = sys.argv[1] if len(sys.argv) > 1 else "PLAIN"
    print(f"{'file':<28} {'function':<38} {'field':<17} {'line':>6} {'k':<2} {'sync':<10} text")
    n = 0
    for r in rows:
        if want != "ALL" and r[5] != want:
            continue
        n += 1
        print(f"{r[0].split('/')[-1]:<28} {r[1]:<38} {r[2]:<17} {r[3]:>6} {r[4]:<2} {r[5]:<10} {r[6]}")
    print(f"\n-- {n} rows shown of {len(rows)} total")
    # summary
    from collections import Counter

    c = Counter((r[0].split("/")[-1], r[5]) for r in rows)
    print("\nsummary (file, sync-class) -> count")
    for k in sorted(c):
        print(f"  {k}: {c[k]}")


if __name__ == "__main__":
    main()

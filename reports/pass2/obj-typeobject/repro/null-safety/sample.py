#!/usr/bin/env python3
"""Enumerate EVERY assignment-from-call site inside the pass-2 regions.

Not a random sample: the full population, stratified by region, so the
hand-check hit rate has a real denominator.  Each row prints the source line
plus the following 6 lines so the check/deref can be judged by eye.
"""
import sys
import re
from pathlib import Path

SCRIPTS = "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts"
sys.path.insert(0, SCRIPTS)
import scan_null_checks as S  # noqa: E402

TARGET = Path("/home/danzin/projects/cpython/Objects/typeobject.c")
raw = TARGET.read_text(encoding="utf-8", errors="replace").split("\n")
src = TARGET.read_text(encoding="utf-8", errors="replace")
stripped = S.strip_comments_and_strings(src)
funcs = S.find_functions(stripped)

REGIONS = [
    ("R3 managed-static", 228, 522), ("R4 accessors", 524, 810),
    ("R6 watchers/versions", 971, 1481), ("R11 MRO C3", 3217, 3702),
    ("R18 PyType_Get*", 5834, 6139), ("R19 lookup cache", 6140, 6452),
    ("R20 setflags", 6453, 6528), ("R21 getattro/setattro", 6529, 6848),
    ("R25 __class__ assign", 7482, 7846), ("R26 pickle", 7848, 8406),
    ("R37 super", 12534, 13068),
]

NOISE = {"sizeof", "if", "while", "for", "switch", "return", "PyTypeObject_CAST",
         "_PyType_CAST", "superobject_CAST", "_PyInterpreterState_GET",
         "_PyThreadState_GET", "PyStackRef_AsPyObjectBorrow",
         "PyStackRef_FromPyObjectSteal", "PyStackRef_FromPyObjectNew",
         "strlen", "strrchr", "strchr", "memcpy", "assert", "Py_ARRAY_LENGTH",
         "PyStackRef_AsPyObjectSteal", "PyStackRef_AsPyObjectNew"}

FROM_CALL = re.compile(
    r"(?P<lval>" + S._LVALUE + r")\s*(?<![=!<>+\-*/%&|^])=(?!=)\s*"
    r"(?:\([^()]*\)\s*)?(?P<api>[A-Za-z_]\w*)\s*\("
)


def region_of(line):
    for name, lo, hi in REGIONS:
        if lo <= line <= hi:
            return name
    return None


rows = []
for f in funcs:
    body = f["body"]
    for m in FROM_CALL.finditer(body):
        api = m.group("api")
        if api in NOISE:
            continue
        line = f["body_line"] + body[:m.start()].count("\n")
        reg = region_of(line)
        if reg is None:
            continue
        rows.append((reg, line, f["name"], m.group("lval").strip(), api))

byreg = {}
for r in rows:
    byreg.setdefault(r[0], []).append(r)

print(f"TOTAL assignment-from-call sites inside pass-2 regions: {len(rows)}\n")
for k, _lo, _hi in REGIONS:
    v = byreg.get(k, [])
    print(f"  {k:24s} {len(v):4d}")
print()

for k, _lo, _hi in REGIONS:
    v = byreg.get(k, [])
    if not v:
        continue
    print(f"\n{'='*78}\n=== {k}  ({len(v)} sites)\n{'='*78}")
    for reg, line, fn, lval, api in sorted(v, key=lambda x: x[1]):
        print(f"\n--- typeobject.c:{line}  {fn}  |  {lval} = {api}(...)")
        for i in range(line - 1, min(line + 6, len(raw))):
            print(f"  {i+1:6d}| {raw[i]}")

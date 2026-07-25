#!/usr/bin/env python3
"""Dump every site the pipeline dropped, with its stage, for hand-checking."""
import sys
import re

SCRIPTS = "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts"
sys.path.insert(0, SCRIPTS)
import scan_null_checks as S  # noqa: E402
from pathlib import Path  # noqa: E402

TARGET = Path("/home/danzin/projects/cpython/Objects/typeobject.c")
src = TARGET.read_text(encoding="utf-8", errors="replace")
stripped = S.strip_comments_and_strings(src)
funcs = S.find_functions(stripped)
extra = S.nullable_source_calls(funcs)
alloc_re = S._alloc_re_for(extra)

# pass-2 regions from include_map.md
REGIONS = [
    ("R3 managed-static", 228, 522), ("R4 accessors", 524, 810),
    ("R6 watchers/versions", 971, 1481), ("R11 MRO C3", 3217, 3702),
    ("R18 PyType_Get*", 5834, 6139), ("R19 lookup cache", 6140, 6452),
    ("R20 setflags", 6453, 6528), ("R21 getattro/setattro", 6529, 6848),
    ("R25 __class__ assign", 7482, 7846), ("R26 pickle", 7848, 8406),
    ("R37 super", 12534, 13068),
]


def region_of(line):
    for name, lo, hi in REGIONS:
        if lo <= line <= hi:
            return name
    return "pass-1 territory"


rows = []
assert_deref_hits = 0
for f in funcs:
    body = f["body"]
    depths = S._depth_profile(body)
    for m in alloc_re.finditer(body):
        api = m.group("api")
        targets = S._assignment_targets(body, m)
        primary = targets[0]
        line = f["body_line"] + body[:m.start()].count("\n")
        if S._in_control_condition(body, m.start()):
            rows.append((line, f["name"], api, primary, "1_control_cond", ""))
            continue
        call_end = S._matching_paren(body, m.end() - 1)
        ws = m.end() if call_end == -1 else call_end + 1
        window = S._truncate_at_reassignment(S._window(body, ws), targets)
        checked_at = None
        for t in targets:
            c = re.search(S._NULL_CHECK_TEMPLATE.format(var=S._lvalue_regex(t)), window)
            if c is not None and (checked_at is None or c.start() < checked_at):
                checked_at = c.start()
        deref = re.search(S._DEREF_TEMPLATE.format(var=S._lvalue_regex(primary)), window)
        if re.search(S._ASSERT_CALL_DEREF.format(var=S._lvalue_regex(primary)), window):
            assert_deref_hits += 1
        if deref is None:
            rows.append((line, f["name"], api, primary, "2_no_deref",
                         "checked" if checked_at is not None else "UNCHECKED"))
            continue
        deref_abs = ws + deref.start()
        dtxt = deref.group(0).strip()
        if not S._dominates(body, depths, m.start(), deref_abs):
            join = S._join_after_full_ifelse(body, m.start(), targets)
            if join is None or not S._dominates(body, depths, join, deref_abs):
                rows.append((line, f["name"], api, primary, "4_not_dominated",
                             f"deref={dtxt!r} "
                             + ("checked" if checked_at is not None else "UNCHECKED")))
                continue
        if checked_at is not None and deref.start() >= checked_at:
            rows.append((line, f["name"], api, primary, "6_checked_first", dtxt))
            continue
        rows.append((line, f["name"], api, primary, "7_FINDING", dtxt))

print(f"_ASSERT_CALL_DEREF matched in a window: {assert_deref_hits} times\n")

print("=== stage 4_not_dominated, UNCHECKED (the interesting suppressions) ===")
for r in rows:
    if r[4] == "4_not_dominated" and "UNCHECKED" in r[5]:
        print(f"  typeobject.c:{r[0]:6d} [{region_of(r[0]):22s}] {r[1]:34s} "
              f"{r[3]} = {r[2]}(...)  {r[5]}")

print("\n=== stage 4_not_dominated, checked ===")
n = 0
for r in rows:
    if r[4] == "4_not_dominated" and "UNCHECKED" not in r[5]:
        n += 1
print(f"  ({n} sites, check present -> not interesting)")

print("\n=== stage 2_no_deref, UNCHECKED (NULL propagates - verify) ===")
for r in rows:
    if r[4] == "2_no_deref" and r[5] == "UNCHECKED":
        print(f"  typeobject.c:{r[0]:6d} [{region_of(r[0]):22s}] {r[1]:34s} "
              f"{r[3]} = {r[2]}(...)")

print("\n=== ALL sites by region ===")
byreg = {}
for r in rows:
    byreg.setdefault(region_of(r[0]), []).append(r)
for k in sorted(byreg):
    print(f"  {k:24s} {len(byreg[k]):4d} matched sites")

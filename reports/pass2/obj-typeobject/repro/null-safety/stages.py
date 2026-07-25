#!/usr/bin/env python3
"""Instrument scan_null_checks' pipeline on Objects/typeobject.c.

Reproduces analyze_function_null_safety stage by stage so we can see exactly
where the 1432-assignment-site denominator collapses to 0 findings.
"""
import sys
import re
from pathlib import Path

SCRIPTS = "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts"
sys.path.insert(0, SCRIPTS)

import scan_null_checks as S  # noqa: E402

TARGET = Path("/home/danzin/projects/cpython/Objects/typeobject.c")
src = TARGET.read_text(encoding="utf-8", errors="replace")
stripped = S.strip_comments_and_strings(src)
funcs = S.find_functions(stripped)
extra = S.nullable_source_calls(funcs)
alloc_re = S._alloc_re_for(extra)

print(f"functions found            : {len(funcs)}")
print(f"local nullable helpers     : {len(extra)}")
print(f"fallible sources resolved  : {len(S.ALLOC_APIS | S.PYOBJ_APIS | extra)}")

# raw assignment_sites metric, as the envelope computes it
assign_sites = sum(len(S._EXTRA_TARGET_RE.findall(f["body"])) for f in funcs)
print(f"assignment_sites (envelope): {assign_sites}")

# how many assignments are assignment-FROM-CALL (pass 1's 760 metric)
FROM_CALL = re.compile(
    r"(?P<lval>" + S._LVALUE + r")\s*(?<![=!<>+\-*/%&|^])=(?!=)\s*"
    r"(?:\([^()]*\)\s*)?(?P<api>[A-Za-z_]\w*)\s*\("
)
from_call = 0
from_call_apis = {}
for f in funcs:
    for m in FROM_CALL.finditer(f["body"]):
        if m.group("api") in ("sizeof", "if", "while", "for", "switch", "return"):
            continue
        from_call += 1
        from_call_apis[m.group("api")] = from_call_apis.get(m.group("api"), 0) + 1
print(f"assignment-from-call sites : {from_call}")

stage = {k: 0 for k in
         ("0_matched", "1_control_cond", "2_no_deref", "3_has_deref",
          "4_not_dominated", "5_join_rescued", "6_checked_first", "7_finding")}
matched_apis = {}
survivors = []
checked_first = []

for f in funcs:
    body = f["body"]
    depths = S._depth_profile(body)
    for m in alloc_re.finditer(body):
        api = m.group("api")
        stage["0_matched"] += 1
        matched_apis[api] = matched_apis.get(api, 0) + 1
        targets = S._assignment_targets(body, m)
        primary = targets[0]
        line = f["body_line"] + body[:m.start()].count("\n")
        if S._in_control_condition(body, m.start()):
            stage["1_control_cond"] += 1
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
        if deref is None:
            stage["2_no_deref"] += 1
            continue
        stage["3_has_deref"] += 1
        deref_abs = ws + deref.start()
        dominated = S._dominates(body, depths, m.start(), deref_abs)
        rescued = False
        if not dominated:
            join = S._join_after_full_ifelse(body, m.start(), targets)
            if join is not None and S._dominates(body, depths, join, deref_abs):
                rescued = True
                stage["5_join_rescued"] += 1
            else:
                stage["4_not_dominated"] += 1
                continue
        if checked_at is not None and deref.start() >= checked_at:
            stage["6_checked_first"] += 1
            checked_first.append((line, f["name"], api, primary))
            continue
        stage["7_finding"] += 1
        survivors.append((line, f["name"], api, primary,
                          deref.group(0).strip(),
                          "unchecked" if checked_at is None else "deref_before_check"))

print("\n--- pipeline stages ---")
for k in sorted(stage):
    print(f"  {k:20s} {stage[k]}")

print(f"\n--- APIs matched by the widened alphabet ({len(matched_apis)} distinct) ---")
for api, n in sorted(matched_apis.items(), key=lambda kv: -kv[1])[:30]:
    tag = "LOCAL" if api in extra else "enum"
    print(f"  {n:4d}  {api}  [{tag}]")

print(f"\n--- 'checked first' drops (proof the check logic runs): {len(checked_first)} ---")
for line, fn, api, var in checked_first[:40]:
    print(f"  typeobject.c:{line}  {fn}  {var} = {api}(...)")

print(f"\n--- SURVIVORS (findings): {len(survivors)} ---")
for s in survivors:
    print("  ", s)

print("\n--- top assignment-from-call callees NOT in the resolved alphabet ---")
resolved = S.ALLOC_APIS | S.PYOBJ_APIS | extra
unresolved = {k: v for k, v in from_call_apis.items() if k not in resolved}
for api, n in sorted(unresolved.items(), key=lambda kv: -kv[1])[:35]:
    print(f"  {n:4d}  {api}")
print(f"  ... {len(unresolved)} distinct unresolved callees, "
      f"{sum(unresolved.values())} sites")

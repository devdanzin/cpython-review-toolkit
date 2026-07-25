import re, sys, collections
sys.path.insert(0, "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts")
import scan_null_checks as S

src = open("/home/danzin/projects/cpython/Objects/typeobject.c", encoding="utf-8", errors="replace").read()
clean = S.strip_comments_and_strings(src)
funcs = S.find_functions(clean)
print("functions found:", len(funcs))

stage = collections.Counter()
apis_reaching_deref = collections.Counter()
for f in funcs:
    body = f["body"]; depths = S._depth_profile(body)
    for m in S._ALL_ALLOC_RE.finditer(body):
        stage["0_matched_enum_assignment"] += 1
        api = m.group("api")
        targets = S._assignment_targets(body, m)
        primary = targets[0]
        if S._in_control_condition(body, m.start()):
            stage["1_dropped_control_condition"] += 1; continue
        call_end = S._matching_paren(body, m.end()-1)
        ws = m.end() if call_end == -1 else call_end+1
        window = S._truncate_at_reassignment(S._window(body, ws), targets)
        checked_at = None
        for t in targets:
            c = re.search(S._NULL_CHECK_TEMPLATE.format(var=S._lvalue_regex(t)), window)
            if c is not None and (checked_at is None or c.start() < checked_at):
                checked_at = c.start()
        deref = re.search(S._DEREF_TEMPLATE.format(var=S._lvalue_regex(primary)), window)
        if deref is None:
            stage["2_dropped_no_deref_in_window"] += 1; continue
        stage["3_has_deref_in_window"] += 1
        apis_reaching_deref[api] += 1
        deref_abs = ws + deref.start()
        if not S._dominates(body, depths, m.start(), deref_abs):
            stage["4_dropped_not_dominated"] += 1; continue
        if checked_at is None:
            stage["5_FINDING_unchecked_alloc"] += 1; continue
        if deref.start() >= checked_at:
            stage["6_dropped_checked_first"] += 1; continue
        stage["7_FINDING_deref_before_check"] += 1

for k in sorted(stage): print(f"  {stage[k]:5d}  {k}")
print()
print("APIs whose result reached a deref-in-window:", dict(apis_reaching_deref))

# rule 3 denominator
wr = S.discover_outparam_wrappers([("Objects/typeobject.c", clean)])
print("outparam wrappers discovered:", wr)
allo = set(S.NULLING_OUTPARAM_APIS) | set(wr)
hits = 0
for a in allo:
    hits += len(re.findall(r"\b"+re.escape(a)+r"\s*\(", clean))
print("rule-3 nulling-outparam call sites in file:", hits)

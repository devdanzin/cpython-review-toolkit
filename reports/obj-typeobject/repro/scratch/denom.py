import re, sys, collections, json
sys.path.insert(0, "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts")
import scan_null_checks as S

src = open("/home/danzin/projects/cpython/Objects/typeobject.c", encoding="utf-8", errors="replace").read()
clean = S.strip_comments_and_strings(src)

KNOWN = S.ALLOC_APIS | S.PYOBJ_APIS

LV = S._LVALUE
# all assignments of the form  lval = SomeCall(
gen = re.compile(r"(?P<lval>" + LV + r")\s*(?<![=!<>+\-*/%&|^])=(?!=)\s*(?:\([^()]*\)\s*)?(?P<api>[A-Za-z_]\w*)\s*\(")

lines = clean.split("\n")
counts = collections.Counter()
known_hits = collections.Counter()
for i, line in enumerate(lines, 1):
    for m in gen.finditer(line):
        api = m.group("api")
        if api in S._C_KEYWORDS: continue
        counts[api] += 1
        if api in KNOWN:
            known_hits[api] += 1

total = sum(counts.values())
known = sum(known_hits.values())
print(f"TOTAL assignment-from-call sites: {total}")
print(f"  resolved by scanner's closed enum: {known}  ({100*known/total:.1f}%)")
print(f"  NOT in the enum: {total-known}")
print()
print("== Scanner-known APIs actually present ==")
for k,v in known_hits.most_common(): print(f"  {v:4d}  {k}")
print()
print("== Top 45 fallible-looking APIs NOT in the enum ==")
for k,v in counts.most_common():
    if k in KNOWN: continue
    print(f"  {v:4d}  {k}")

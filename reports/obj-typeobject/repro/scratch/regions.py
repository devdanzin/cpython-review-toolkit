import re, os, collections, datetime
SD = "/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad"

CLASSES = [
 ("use-after-free", r"use[- ]after[- ]free|\buaf\b|dangling|freed memory|after it (?:is|was) freed"),
 ("double-free",    r"double[- ]free|freed twice|double decref|double[- ]dealloc"),
 ("data-race",      r"data race|\brace\b|thread[- ]saf|free[- ]?thread|tsan|atomic|critical section|deadlock|lock order|_Py_atomic|GIL"),
 ("refcount",       r"refcount|reference count|refleak|ref leak|incref|decref|borrowed ref|stolen ref|immortal"),
 ("memory-leak",    r"\bleak|leaked|leaking"),
 ("null-deref",     r"\bnull\b|NULL deref|nullptr|missing .{0,20}check"),
 ("crash",          r"crash|segfault|segv|sigsegv|abort|fatal error|hang|infinite loop|stack overflow"),
 ("recursion",      r"recursion|recursive|deep(?:ly)? nested|RecursionError"),
 ("overflow",       r"overflow|underflow|out[- ]of[- ]bounds|buffer over|integer over"),
 ("uninitialized",  r"uninitiali[sz]ed|garbage value|not initialized"),
 ("assertion",      r"assertion|assert fail|_PyObject_ASSERT"),
 ("corruption",     r"corrupt|inconsisten|invalid state"),
]
FIXRE = re.compile(r"\bfix|\bbug|\bgh-\d+|\bbpo-\d+|\bissue\s*#?\d+|regression|revert", re.I)

def classes_of(subj):
    out=[]
    for name, pat in CLASSES:
        if re.search(pat, subj, re.I): out.append(name)
    return out

regions=[]
for line in open(f"{SD}/regions.txt"):
    n,a,b,name = line.strip().split("|")
    regions.append((int(n), int(a), int(b), name))

CUT12 = "2025-07-25"; CUT36 = "2023-07-25"; CUT60="2021-07-25"
rows=[]
cluster_commits = collections.defaultdict(dict)  # class -> hash -> (date, subj, regions)
for n,a,b,name in regions:
    lines=[l.strip() for l in open(f"{SD}/region_{n}.txt") if l.strip()]
    seen=set(); commits=[]
    for l in lines:
        h,d,s = l.split("|",2)
        if h in seen: continue
        seen.add(h); commits.append((h,d,s))
    kloc = (b-a+1)/1000.0
    fixes = [c for c in commits if FIXRE.search(c[2])]
    crash = [c for c in commits if classes_of(c[2])]
    crash12 = [c for c in crash if c[1] >= CUT12]
    crash36 = [c for c in crash if c[1] >= CUT36]
    crash60 = [c for c in crash if c[1] >= CUT60]
    all12 = [c for c in commits if c[1] >= CUT12]
    for c in crash:
        for cl in classes_of(c[2]):
            cluster_commits[cl][c[0]] = (c[1], c[2], n)
    rows.append(dict(n=n,a=a,b=b,name=name,kloc=kloc,total=len(commits),
        fixes=len(fixes),crash=len(crash),c12=len(crash12),c36=len(crash36),c60=len(crash60),
        all12=len(all12),
        dens_all=round(len(crash)/kloc,2), dens60=round(len(crash60)/kloc,2),
        dens36=round(len(crash36)/kloc,2), dens12=round(len(crash12)/kloc,2),
        top=[c for c in crash[:6]]))

print("=== RANKED BY 5-YEAR CRASH-FIX DENSITY (crash-shaped fixes since 2021-07 per KLOC) ===")
print(f"{'#':>3} {'lines':>12} {'kloc':>5} {'tot':>4} {'crashAll':>8} {'c60':>4} {'c36':>4} {'c12':>4} {'d60':>6} {'d36':>6} {'d12':>6}  region")
for r in sorted(rows,key=lambda r:(-r['dens60'],-r['dens36'])):
    print(f"{r['n']:>3} {str(r['a'])+'-'+str(r['b']):>12} {r['kloc']:>5.2f} {r['total']:>4} {r['crash']:>8} {r['c60']:>4} {r['c36']:>4} {r['c12']:>4} {r['dens60']:>6.2f} {r['dens36']:>6.2f} {r['dens12']:>6.2f}  {r['name']}")

print()
print("=== CLUSTERS (whole file, dedup by hash) ===")
for cl,_ in CLASSES:
    dd = cluster_commits.get(cl,{})
    if not dd: continue
    years = collections.Counter(v[0][:4] for v in dd.values())
    recent = sorted(dd.items(), key=lambda kv:-ord(kv[1][0][0]) if False else kv[1][0], reverse=True)[:6]
    hist = " ".join(f"{y}:{years[y]}" for y in sorted(years) if y>="2019")
    print(f"\n--{cl}-- total={len(dd)}  [{hist}]")
    for h,(d,s,rn) in recent:
        print(f"   {h[:12]} {d} R{rn:<2} {s[:105]}")

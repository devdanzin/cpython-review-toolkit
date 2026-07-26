"""How much would seeding the guard set from _Py_CRITICAL_SECTION_ASSERT_* buy?

For every function in Objects/ + Modules/ + Python/ whose body asserts the object
is locked, check whether the scanner already knows it is guarded by any of its
three existing mechanisms (name suffix / clinic @critical_section / _has_lock)
or by transitive call-site propagation.
"""
import sys
from pathlib import Path
sys.path.insert(0,"/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts")
import scan_ft_races as sfr, tree_sitter_utils as tsu

CPY=Path("/home/danzin/projects/cpython")
tot=0; by_name=0; by_clinic=0; by_haslock=0; by_prop=0; uncovered=[]
for root in ("Objects","Modules","Python"):
    for p in sorted((CPY/root).rglob("*.c")):
        b=p.read_bytes()
        try: tree=tsu.parse_bytes(b)
        except Exception: continue
        funcs=tsu.extract_functions(tree,b)
        if not funcs: continue
        src=b.decode("utf-8","replace")
        clinic=sfr._clinic_guarded_functions(src,funcs)
        stripped=sfr.strip_comments(src)
        cs=sfr._critical_section_spans(stripped)
        gil_only,_=sfr._gil_disabled_regions(src)
        prop=sfr._caller_propagated_guards(stripped,funcs,cs,gil_only,clinic)
        for f in funcs:
            body=sfr.strip_comments(f["body"])
            if "_Py_CRITICAL_SECTION_ASSERT" not in body: continue
            tot+=1
            n=f["name"]
            if sfr._caller_holds_lock(n): by_name+=1
            elif n in clinic: by_clinic+=1
            elif sfr._has_lock(f["body"]): by_haslock+=1
            elif n in prop: by_prop+=1
            else: uncovered.append(f"{p.relative_to(CPY)}:{f['start_line']} {n}")
print(f"functions asserting CRITICAL_SECTION_ASSERT: {tot}")
print(f"  already covered by name suffix   : {by_name}")
print(f"  already covered by clinic guard  : {by_clinic}")
print(f"  already covered by _has_lock     : {by_haslock}")
print(f"  already covered by propagation   : {by_prop}")
print(f"  NOT covered by anything          : {len(uncovered)}")
for u in uncovered: print("     ",u)

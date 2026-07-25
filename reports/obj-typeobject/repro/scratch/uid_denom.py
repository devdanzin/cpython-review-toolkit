"""Compute the denominator: which object-allocation sites in a file the
scan_uninit_dealloc rule actually resolves."""
import re
import sys
from pathlib import Path

SCRIPTS = "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts"
sys.path.insert(0, SCRIPTS)

import scan_uninit_dealloc as S
from tree_sitter_utils import (
    extract_functions,
    find_assignments_in_scope,
    find_calls_in_scope,
    parse_bytes,
)

path = Path(sys.argv[1])
src = path.read_bytes()
tree = parse_bytes(src)
funcs = extract_functions(tree, src)
text = src.decode("utf-8", "replace")

local_nz = S._nonzeroing_tp_allocs(text, funcs)
print(f"file: {path}")
print(f"functions extracted: {len(funcs)}")
print(f"_nonzeroing_tp_allocs (file-local non-zeroing allocfuncs): {sorted(local_nz)}")

registered = {
    name
    for m in S._TP_ALLOC_SLOT_RE.finditer(text)
    for name in m.groups()
    if name and name not in ("0", "NULL")
}
print(f"registered tp_alloc slot names in file: {sorted(registered)}")

OBJ_ALLOC_SHAPE = re.compile(
    r"(?:->|\.)\s*tp_alloc\s*\(|"
    r"\b(?:PyObject_New|PyObject_NewVar|PyObject_GC_New|PyObject_GC_NewVar"
    r"|_PyObject_New|_PyObject_NewVar|_PyObject_GC_New|_PyObject_GC_NewVar"
    r"|PyObject_GC_NewWithExtra|PyType_GenericAlloc|_PyType_AllocNoTrack"
    r"|_PyObject_GC_Malloc|_PyObject_MallocWithType)\s*\("
)

seen = set()
rows = []
for f in funcs:
    frees = sorted(
        find_calls_in_scope(f["body_node"], src, S._EARLY_FREE_NAMES),
        key=lambda c: c["start_byte"],
    )
    for a in find_assignments_in_scope(f["body_node"], src):
        v = a["value_text"]
        if not OBJ_ALLOC_SHAPE.search(v):
            continue
        api = S._matched_allocator(v, local_nz)
        alloc_end = a["value_node"].end_byte
        has_free = any(
            c["start_byte"] >= alloc_end
            and S._bare_ident(c["arguments_text"]) == a["variable"]
            for c in frees
        )
        key = (a["start_line"], a["variable"])
        if key in seen:
            continue
        seen.add(key)
        rows.append((a["start_line"], f["name"], a["variable"], v.strip()[:55], api, has_free))

rows.sort()
print(f"\nobject-allocation-shaped assignment sites the AST walk sees: {len(rows)}")
print(f"{'line':>6}  {'function':<28} {'var':<9} {'resolved':<16} {'free?':<6} rhs")
for line, fn, var, v, api, hf in rows:
    print(f"{line:>6}  {fn:<28} {var:<9} {str(api):<16} {str(hf):<6} {v}")

resolved = sum(1 for r in rows if r[4] is not None)
print(f"\nRESOLVED   (rule considers): {resolved} / {len(rows)}")
print(f"UNRESOLVED (rule skips)    : {len(rows) - resolved} / {len(rows)}")
print(f"\nraw tp_alloc virtual-call sites in file: {len(S._TP_ALLOC_CALL_RE.findall(text))}")

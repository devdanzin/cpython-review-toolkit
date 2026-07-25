"""Honest allocation-site census for Objects/typeobject.c.

Reuses the scanner's own AST helpers so the count is comparable to the
`allocation_sites` denominator it reports.
"""

import re
import sys

sys.path.insert(
    0, "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts"
)

import scan_uninit_dealloc as S  # noqa: E402
from tree_sitter_utils import (  # noqa: E402
    extract_functions,
    find_assignments_in_scope,
    find_calls_in_scope,
    parse_bytes,
)

PATH = "/home/danzin/projects/cpython/Objects/typeobject.c"

# every family that hands back storage the caller must initialise
ALLOC_RE = re.compile(
    r"\b("
    r"PyObject_New|PyObject_NewVar|PyObject_GC_New|PyObject_GC_NewVar|"
    r"PyObject_GC_NewWithExtra|_PyObject_New|_PyObject_NewVar|_PyObject_GC_New|"
    r"_PyObject_GC_NewVar|_PyObject_GC_Malloc|_PyObject_GC_Resize|"
    r"PyMem_New|PyMem_Malloc|PyMem_Calloc|PyMem_Realloc|PyMem_Resize|"
    r"PyMem_RawMalloc|PyMem_RawCalloc|PyMem_RawRealloc|"
    r"PyObject_Malloc|PyObject_Calloc|PyObject_Realloc|"
    r"_PyObject_MallocWithType|_PyObject_ReallocWithType|"
    r"PyType_GenericAlloc|_PyType_AllocNoTrack|"
    r"malloc|calloc|realloc"
    r")\s*\(|"
    r"\w+\s*(?:->|\.)\s*tp_alloc\s*\("
)


def main() -> None:
    src = open(PATH, encoding="utf-8", errors="replace").read()
    sb = src.encode("utf-8")
    tree = parse_bytes(sb)
    funcs = extract_functions(tree, sb)
    local_nz = S._nonzeroing_tp_allocs(src, funcs)
    print(f"functions extracted: {len(funcs)}")
    print(f"file-local non-zeroing allocfuncs: {sorted(local_nz)}")

    rows = []
    for f in funcs:
        body = f["body_node"]
        frees = find_calls_in_scope(body, sb, S._EARLY_FREE_NAMES)
        free_lines = sorted(c["start_line"] for c in frees)
        for a in find_assignments_in_scope(body, sb):
            vt = a["value_text"]
            if not ALLOC_RE.search(vt):
                continue
            resolved = S._matched_allocator(vt, local_nz)
            rows.append(
                {
                    "line": a["start_line"],
                    "func": f["name"],
                    "var": a["variable"],
                    "expr": " ".join(vt.split())[:70],
                    "resolved": resolved,
                    "frees": free_lines,
                }
            )
    rows.sort(key=lambda r: r["line"])
    print(f"\ncandidate allocation sites found by hand-regex: {len(rows)}")
    print(f"of which the scanner resolves: {sum(1 for r in rows if r['resolved'])}")
    print()
    hdr = f"{'line':>6}  {'function':<32} {'var':<12} {'resolved':<10} {'frees?':<6} expr"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{r['line']:>6}  {r['func']:<32} {r['var']:<12} "
            f"{str(r['resolved']):<10} {('yes' if r['frees'] else 'no'):<6} {r['expr']}"
        )
        if r["frees"]:
            print(f"{'':>6}  {'':<32} early-free lines in fn: {r['frees']}")


if __name__ == "__main__":
    main()

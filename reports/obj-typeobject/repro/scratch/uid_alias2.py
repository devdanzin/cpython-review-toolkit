"""Measure recall hole #2: how often a constructor allocates into V but writes
members through an ALIAS of V, which _member_writes() cannot see."""
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

roots = [Path(p) for p in sys.argv[1:]]
alias_hits = []
n_alloc_sites = 0
n_with_free = 0
n_no_writes = 0

for root in roots:
    for p in sorted(root.rglob("*.c")):
        try:
            src = p.read_bytes()
            tree = parse_bytes(src)
        except Exception:
            continue
        funcs = extract_functions(tree, src)
        if not funcs:
            continue
        local_nz = S._nonzeroing_tp_allocs(src.decode("utf-8", "replace"), funcs)
        for f in funcs:
            frees = sorted(
                find_calls_in_scope(f["body_node"], src, S._EARLY_FREE_NAMES),
                key=lambda c: c["start_byte"],
            )
            for a in find_assignments_in_scope(f["body_node"], src):
                api = S._matched_allocator(a["value_text"], local_nz)
                if api is None:
                    continue
                n_alloc_sites += 1
                var = a["variable"]
                alloc_end = a["value_node"].end_byte
                if not any(
                    c["start_byte"] >= alloc_end
                    and S._bare_ident(c["arguments_text"]) == var
                    for c in frees
                ):
                    continue
                n_with_free += 1
                writes = [
                    m for m, n in S._member_writes(f["body_node"], src, var)
                    if n.start_byte >= alloc_end
                ]
                body = f["body"]
                # alias declaration of the allocated var
                al = re.findall(
                    r"\b(\w+)\s*=\s*\(?\s*(?:\([\w\s*]+\)\s*)?&?\s*"
                    + re.escape(var) + r"\s*(?:->\s*\w+)?\s*;",
                    body,
                )
                al = [x for x in al if x != var]
                if not writes:
                    print("NOWRITES", p, f["name"], a["start_line"], var, api)
                    n_no_writes += 1
                if al:
                    alias_hits.append(
                        (str(p), f["name"], a["start_line"], var, api, al, len(writes))
                    )

print(f"resolved non-zeroing alloc sites: {n_alloc_sites}")
print(f"  ... of which have an early free of the same var: {n_with_free}")
print(f"  ... of those, ZERO in-scope member writes through the var: {n_no_writes}")
print(f"\nsites where an ALIAS variable of the allocation exists ({len(alias_hits)}):")
for f_, fn, line, var, api, al, nw in alias_hits:
    print(f"  {f_}:{line} {fn}  var={var} alias={al} in-scope-writes={nw}")

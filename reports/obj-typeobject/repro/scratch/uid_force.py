"""Re-run scan_uninit_dealloc with tp_alloc FORCED non-zeroing, to measure what
the structural skip hides. Also dumps the raw unset_members_all before the
destructor-evidence filter."""
import re
import sys
from pathlib import Path

SCRIPTS = "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts"
sys.path.insert(0, SCRIPTS)

import scan_uninit_dealloc as S
from tree_sitter_utils import extract_functions, parse_bytes

_orig = S._matched_allocator


def forced(value_text, local_nonzeroing):
    r = _orig(value_text, local_nonzeroing)
    if r is not None:
        return r
    head = S._strip_lead(value_text)
    if re.match(r"\w+\s*(?:->|\.)\s*tp_alloc\s*\(", head):
        return "tp_alloc(FORCED)"
    return None


S._matched_allocator = forced

path = Path(sys.argv[1])
src = path.read_bytes()
tree = parse_bytes(src)
funcs = extract_functions(tree, src)
evidence = S._member_evidence(src, funcs)
local_nz = S._nonzeroing_tp_allocs(src.decode("utf-8", "replace"), funcs)

print(f"destructor-evidence members ({len(evidence)}):")
for k, v in sorted(evidence.items()):
    print(f"   {k:<28} {v[0]:<16} line {v[1]}")

print("\n--- findings with tp_alloc forced non-zeroing ---")
n = 0
for f in funcs:
    for fi in S._check_function(f, src, tree, evidence, local_nz):
        n += 1
        print(f"\n[{n}] {fi['function']} line {fi['line']} alloc={fi['allocator']} "
              f"var={fi['variable']} free_line={fi['free_line']} ({fi['free_call']})")
        print(f"    unset_members     : {fi['unset_members']}")
        print(f"    unset_members_all : {fi['unset_members_all']}")
        print(f"    confidence        : {fi['confidence']}")
        print(f"    evidence          : {fi['destructor_evidence']}")
if n == 0:
    print("(none)")

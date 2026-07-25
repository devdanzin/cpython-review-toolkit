"""Tree-wide: every registered tp_alloc slot name, and which of them are
non-zeroing per the scanner's own predicate."""
import sys
from pathlib import Path

SCRIPTS = "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts"
sys.path.insert(0, SCRIPTS)

import scan_uninit_dealloc as S
from tree_sitter_utils import extract_functions, parse_bytes

root = Path(sys.argv[1])
allnames = {}
nonzero = {}
for p in sorted(root.rglob("*.c")):
    try:
        src = p.read_bytes()
    except OSError:
        continue
    text = src.decode("utf-8", "replace")
    reg = {
        n
        for m in S._TP_ALLOC_SLOT_RE.finditer(text)
        for n in m.groups()
        if n and n not in ("0", "NULL")
    }
    if not reg:
        continue
    for n in reg:
        allnames.setdefault(n, []).append(str(p.relative_to(root)))
    try:
        funcs = extract_functions(parse_bytes(src), src)
    except Exception:
        continue
    nz = S._nonzeroing_tp_allocs(text, funcs)
    for n in nz:
        nonzero.setdefault(n, []).append(str(p.relative_to(root)))

print("=== all registered tp_alloc slot values, tree-wide ===")
for n, files in sorted(allnames.items()):
    print(f"  {n:<28} {len(files)} file(s): {files[:4]}")
print("\n=== detected NON-ZEROING tp_alloc implementations ===")
for n, files in sorted(nonzero.items()):
    print(f"  {n:<28} {files}")
if not nonzero:
    print("  (none)")

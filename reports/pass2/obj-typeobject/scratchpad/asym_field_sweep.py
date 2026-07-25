"""Find PyTypeObject fields WRITTEN in more than one place where at least one
write is inside a TYPE_LOCK / stop-the-world region and at least one is not.

That asymmetry is what scan_lock_discipline structurally cannot see: it checks
acquire/release balance, never "is this field's writer set consistently guarded".
"""

import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(
    0, "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts"
)
import tree_sitter_utils as tsu  # noqa: E402

SRC = Path("/home/danzin/projects/cpython/Objects/typeobject.c")

ACQ = ("BEGIN_TYPE_LOCK", "BEGIN_TYPE_DICT_LOCK")
REL = ("END_TYPE_LOCK", "END_TYPE_DICT_LOCK")
STW_A, STW_R = "types_stop_world", "types_start_world"

# fields we care about: PyTypeObject / PyHeapTypeObject state
FIELD_RE = re.compile(
    r"\b(?:self|type|tp|ty|et|base|subclass|child)\s*->\s*(tp_[a-z_]+|_spec_cache)\b"
    r"\s*(\+=|-=|\|=|&=|\^=|=(?!=))"
)


def main():
    raw = SRC.read_bytes()
    lines = raw.decode("utf-8", "replace").splitlines()
    tree = tsu.parse_bytes_for_file(raw, SRC)
    spans = sorted((f["start_line"], f["end_line"], f["name"]) for f in tsu.extract_functions(tree, raw))

    def fn_of(n):
        hit = "<file>"
        for s, e, nm in spans:
            if s <= n <= e:
                hit = nm
        return hit

    def nocomment(i):
        return re.sub(r"//.*$", "", lines[i - 1])

    # build lock-region and stw-region line sets
    locked, stw = set(), set()
    depth = 0
    for i in range(1, len(lines) + 1):
        t = nocomment(i)
        if lines[i - 1].lstrip().startswith("#define"):
            continue
        if any(re.search(r"\b" + a + r"\b", t) for a in ACQ):
            depth += 1
        if depth > 0:
            locked.add(i)
        if any(re.search(r"\b" + r_ + r"\b", t) for r_ in REL):
            depth = max(0, depth - 1)
    d2 = 0
    for i in range(1, len(lines) + 1):
        t = nocomment(i)
        if lines[i - 1].lstrip().startswith("#define"):
            continue
        if re.search(r"\b" + STW_A + r"\b", t):
            d2 += 1
        if d2 > 0:
            stw.add(i)
        if re.search(r"\b" + STW_R + r"\b", t):
            d2 = max(0, d2 - 1)

    writes = defaultdict(list)
    for i in range(1, len(lines) + 1):
        if lines[i - 1].lstrip().startswith("#define"):
            continue
        t = nocomment(i)
        m = FIELD_RE.search(t)
        if not m:
            continue
        field = m.group(1)
        writes[field].append((i, fn_of(i), i in locked, i in stw, t.strip()))

    print("field                writes  under_TYPE_LOCK  under_STW  bare")
    print("-" * 78)
    asym = []
    for field, ws in sorted(writes.items()):
        nl = sum(1 for w in ws if w[2])
        ns = sum(1 for w in ws if w[3])
        bare = sum(1 for w in ws if not w[2] and not w[3])
        print(f"{field:20s} {len(ws):5d} {nl:14d} {ns:10d} {bare:6d}")
        if (nl or ns) and bare:
            asym.append(field)

    print("\n===== ASYMMETRIC FIELDS (guarded somewhere, bare elsewhere) =====")
    for field in asym:
        print(f"\n--- {field} ---")
        for ln, fn, lk, sw, txt in writes[field]:
            tag = "TYPE_LOCK" if lk else ("STW" if sw else "*** BARE ***")
            print(f"  :{ln:<6d} {tag:14s} {fn:36s} | {txt[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Pass-2 lock-discipline certification for Objects/typeobject.c, tree-sitter backed.

Certifies the scanner's denominator (critical_section_functions=23, mutex_functions=3)
and hand-verifies every acquire/release pair for early exits.
"""

import re
import sys
from pathlib import Path

sys.path.insert(
    0, "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts"
)
import tree_sitter_utils as tsu  # noqa: E402

SRC = Path("/home/danzin/projects/cpython/Objects/typeobject.c")

ACQ = {
    "BEGIN_TYPE_LOCK": ("critical_section", "END_TYPE_LOCK"),
    "BEGIN_TYPE_DICT_LOCK": ("critical_section", "END_TYPE_DICT_LOCK"),
    "Py_BEGIN_CRITICAL_SECTION": ("critical_section", "Py_END_CRITICAL_SECTION"),
    "PyMutex_Lock": ("pymutex", "PyMutex_Unlock"),
}
STW = {
    "types_stop_world": "types_start_world",
    "_PyEval_StopTheWorld": "_PyEval_StartTheWorld",
    "type_lock_prevent_release": "type_lock_allow_release",
}


def main():
    raw = SRC.read_bytes()
    lines = raw.decode("utf-8", "replace").splitlines()
    tree = tsu.parse_bytes_for_file(raw, SRC)
    funcs = tsu.extract_functions(tree, raw)
    spans = sorted(
        (f["start_line"], f["end_line"], f["name"]) for f in funcs
    )
    print(f"tree-sitter extract_functions: {len(funcs)} functions")

    def enclosing(lineno):
        hit = None
        for s, e, n in spans:
            if s <= lineno <= e:
                hit = (n, s, e)
        return hit or ("<file-scope>", 0, 0)

    def code_line(i):
        """1-based; True if the line has code outside a // comment."""
        t = lines[i - 1]
        t = re.sub(r"//.*$", "", t)
        return t

    cs_funcs, mx_funcs = set(), set()
    problems = []
    all_regions = []

    for label, table in (("LOCK", ACQ), ("STW", {k: (None, v) for k, v in STW.items()})):
        print(f"\n===== {label} =====")
        open_by_rel = {}
        events = []
        for i, ln in enumerate(lines, start=1):
            if ln.lstrip().startswith("#define"):
                continue
            c = code_line(i)
            for acq, (_fam, rel) in table.items():
                if re.search(r"\b" + re.escape(acq) + r"\b", c):
                    events.append((i, "ACQ", acq, rel))
                if re.search(r"\b" + re.escape(rel) + r"\b", c):
                    events.append((i, "REL", acq, rel))
        for lineno, kind, acq, rel in events:
            if kind == "ACQ":
                open_by_rel.setdefault(rel, []).append((lineno, acq))
            else:
                st = open_by_rel.get(rel) or []
                if not st:
                    problems.append(f"UNPAIRED RELEASE {rel} :{lineno}")
                    continue
                a_line, a_tok = st.pop()
                fn, fs, fe = enclosing(a_line)
                fn2, _, _ = enclosing(lineno)
                if label == "LOCK":
                    fam = ACQ[a_tok][0]
                    (cs_funcs if fam == "critical_section" else mx_funcs).add((fn, fs))
                exits = []
                for k in range(a_line + 1, lineno):
                    c = code_line(k)
                    for kw in ("return", "goto"):
                        if re.search(r"\b" + kw + r"\b", c):
                            exits.append((k, kw, c.strip()))
                all_regions.append((label, a_tok, a_line, rel, lineno, fn, exits))
                mark = "  <<<EXIT" if exits else ""
                same = "" if fn == fn2 else f"  !!CROSS->{fn2}"
                print(f"  {a_tok:26s}:{a_line:<6d}-> {rel:24s}:{lineno:<6d} {fn}{same}{mark}")
                for e in exits:
                    print(f"        :{e[0]:<6d} {e[1]:6s} | {e[2][:100]}")
                if fn != fn2:
                    problems.append(f"CROSS-FN {a_tok}:{a_line} -> {rel}:{lineno}")
        for rel, st in open_by_rel.items():
            for a_line, a_tok in st:
                problems.append(f"UNPAIRED ACQUIRE {a_tok} :{a_line}")

    print("\n===== DENOMINATOR CERTIFICATION =====")
    print(f"critical_section functions: {len(cs_funcs)}")
    for n, s in sorted(cs_funcs, key=lambda x: x[1]):
        print(f"    :{s:<6d} {n}")
    print(f"pymutex functions: {len(mx_funcs)}")
    for n, s in sorted(mx_funcs, key=lambda x: x[1]):
        print(f"    :{s:<6d} {n}")

    print("\n===== PROBLEMS =====")
    print("  none" if not problems else "\n".join("  " + p for p in problems))
    return 0


if __name__ == "__main__":
    sys.exit(main())

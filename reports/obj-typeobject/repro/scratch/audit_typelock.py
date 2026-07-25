"""Audit BEGIN_TYPE_LOCK/END_TYPE_LOCK regions in typeobject.c for early exits.

This is the check scan_lock_discipline.py cannot do, because BEGIN_TYPE_LOCK is a
file-local alias for Py_BEGIN_CRITICAL_SECTION_MUTEX that the scanner's macro
vocabulary does not resolve.
"""

import re
import sys

PATH = "/home/danzin/projects/cpython/Objects/typeobject.c"

BEGINS = {
    "BEGIN_TYPE_LOCK": "END_TYPE_LOCK",
    "BEGIN_TYPE_DICT_LOCK": "END_TYPE_DICT_LOCK",
    "Py_BEGIN_CRITICAL_SECTION": "Py_END_CRITICAL_SECTION",
    "types_stop_world": "types_start_world",
    "type_lock_prevent_release": "type_lock_allow_release",
}

# lines that are macro definitions, not uses
DEFINE = re.compile(r"^\s*#\s*define\b")


def main() -> int:
    lines = open(PATH, encoding="utf-8", errors="replace").read().splitlines()

    # find function start lines (crude: a line starting at col 0 with '{' preceded
    # by a signature) -- we only need it to bound the search.
    for begin, end in BEGINS.items():
        print(f"\n{'='*72}\n{begin}  ->  {end}\n{'='*72}")
        stack = []
        pairs = []
        for i, ln in enumerate(lines, 1):
            if DEFINE.match(ln):
                continue
            # word-boundary match on the call
            if re.search(rf"\b{begin}\b", ln):
                stack.append(i)
            if re.search(rf"\b{end}\b", ln):
                if stack:
                    pairs.append((stack.pop(), i))
                else:
                    print(f"  !! UNMATCHED {end} at line {i}")
        for leftover in stack:
            print(f"  !! UNMATCHED {begin} at line {leftover}")

        pairs.sort()
        print(f"  {len(pairs)} pairs")
        for b, e in pairs:
            body = lines[b:e - 1]  # exclusive of the begin/end lines themselves
            exits = []
            for off, bl in enumerate(body):
                stripped = bl.strip()
                if stripped.startswith("//") or stripped.startswith("*"):
                    continue
                if re.search(r"\breturn\b", bl):
                    exits.append((b + 1 + off, "return", stripped))
                elif re.search(r"\bgoto\s+(\w+)", bl):
                    exits.append((b + 1 + off, "goto", stripped))
            span = e - b
            if exits:
                print(f"\n  -- pair {b}..{e}  (span {span})  {len(exits)} exit-ish lines")
                for ln_no, kind, txt in exits:
                    print(f"       {ln_no:>6} {kind:6} {txt[:100]}")
            else:
                print(f"  ok pair {b}..{e}  (span {span}) no return/goto inside")
    return 0


if __name__ == "__main__":
    sys.exit(main())

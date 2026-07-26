"""Enumerate sentinel returns and pre-classify whether an exception is provably set.

For each `return NULL;` / `return -1;` / `return -2;` in the four slice files,
walk backwards through the enclosing block collecting the nearest evidence that
an exception is (or is not) set, and bucket the site.
"""

import re
import sys

FILES = [
    "Objects/listobject.c",
    "Objects/bytesobject.c",
    "Objects/bytearrayobject.c",
    "Objects/bytes_methods.c",
]

ROOT = "/home/danzin/projects/cpython/"

SENTINEL = re.compile(r"^\s*return\s+(NULL|-1|-2)\s*;")
RAISER = re.compile(
    r"PyErr_Set|PyErr_Format|PyErr_NoMemory|_PyErr_Set|_PyErr_Format|"
    r"PyErr_BadInternalCall|PyErr_BadArgument|PyErr_Occurred|_PyErr_Occurred|"
    r"PyErr_ExceptionMatches"
)
# Calls that set an exception themselves on failure
CALL = re.compile(r"\b(_?Py[A-Za-z_]*|_Py[A-Za-z_]*)\s*\(")


def enclosing_function(lines, idx):
    for j in range(idx, -1, -1):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_ \*]*)?\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", lines[j])
        if m and lines[j][0] not in " \t#/*" and "=" not in lines[j].split("(")[0]:
            return m.group(2)
    return "?"


def main():
    total = 0
    buckets = {}
    for f in FILES:
        path = ROOT + f
        lines = open(path, encoding="utf-8", errors="replace").read().split("\n")
        for i, line in enumerate(lines):
            m = SENTINEL.match(line)
            if not m:
                continue
            total += 1
            # look back up to 8 lines for evidence
            window = lines[max(0, i - 8):i]
            wtext = "\n".join(window)
            has_raiser = bool(RAISER.search(wtext))
            has_call = bool(CALL.search(wtext))
            if has_raiser:
                b = "A_explicit_raise_or_occurred_test"
            elif has_call:
                b = "B_failed_api_call_nearby"
            else:
                b = "C_NO_EVIDENCE"
            buckets.setdefault(b, []).append(
                (f, i + 1, enclosing_function(lines, i), line.strip(), window[-4:])
            )
    print(f"TOTAL sentinel returns: {total}")
    for b in sorted(buckets):
        print(f"\n### {b}: {len(buckets[b])}")
    print("\n\n===== BUCKET C (no evidence within 8 lines) =====")
    for f, ln, fn, txt, ctx in buckets.get("C_NO_EVIDENCE", []):
        print(f"\n--- {f}:{ln}  [{fn}]  {txt}")
        for c in ctx:
            print("    " + c.rstrip())


if __name__ == "__main__":
    sys.exit(main())

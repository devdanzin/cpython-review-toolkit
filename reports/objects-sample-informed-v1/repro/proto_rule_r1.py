"""Prototype of proposed scan_null_checks.py rule: decref_of_nulled_outparam.

Shape: an API that NULLs its PyObject** out-param on failure, followed by
Py_DECREF(that var) inside the failure branch  ->  guaranteed Py_DECREF(NULL).

Two passes:
  pass 1: direct calls to the known NULLing APIs
  pass 2: discover local wrappers (fn that takes PyObject** and forwards it to
          a NULLing API, returning a failure sentinel), then re-run on those.
"""

import re
import pathlib
import sys
from collections import defaultdict

SEED = ["_PyTuple_Resize", "_PyBytes_Resize", "PyUnicode_Resize", "_PyUnicode_Resize"]
ROOTS = ["Objects", "Modules", "Python", "Parser"]
CPY = pathlib.Path("/home/danzin/projects/cpython")

DEC = re.compile(r"\bPy_DECREF\s*\(\s*([A-Za-z_][\w\->.]*)\s*\)")


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                 src, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", src)


def find_wrappers(files):
    """Find static fns whose PyObject** parameter is forwarded to a NULLing API."""
    wrappers = {}
    fndef = re.compile(
        r"^(?:[A-Za-z_][\w \t\*]*?[\s\*])?(\w+)\s*\(([^;{]*?)\)\s*\n\{", re.M)
    seed_re = re.compile(r"\b(" + "|".join(SEED) + r")\s*\(\s*(\w+)\s*[,)]")
    for p, src in files:
        for m in fndef.finditer(src):
            name, params = m.group(1), m.group(2)
            pm = re.findall(r"PyObject\s*\*\s*\*\s*(\w+)", params)
            if not pm:
                continue
            body = src[m.end(): m.end() + 2500]
            for sm in seed_re.finditer(body):
                if sm.group(2) in pm:
                    wrappers[name] = (str(p), pm.index(sm.group(2)))
    return wrappers


def scan(files, apis):
    call = re.compile(
        r"\b(" + "|".join(re.escape(a) for a in apis) + r")\s*\(\s*&\s*([A-Za-z_][\w\->.]*)")
    hits = []
    for p, src in files:
        lines = src.splitlines()
        for i, ln in enumerate(lines):
            m = call.search(ln)
            if not m:
                continue
            var = m.group(2)
            for j in range(i, min(i + 16, len(lines))):
                for dm in DEC.finditer(lines[j]):
                    if dm.group(1) == var:
                        hits.append((str(p), i + 1, j + 1, m.group(1), var,
                                     lines[j].strip()))
    return hits


def main():
    files = []
    for d in ROOTS:
        for p in (CPY / d).rglob("*.c"):
            files.append((p.relative_to(CPY), strip_comments(
                p.read_text(errors="replace"))))
    wr = find_wrappers(files)
    print("discovered wrappers:", sorted(wr))
    apis = SEED + sorted(wr)
    hits = scan(files, apis)
    print(f"\ncandidates ({len(hits)}):")
    for h in hits:
        print(f"  {h[0]}:{h[1]}  {h[3]}(&{h[4]})  ->  :{h[2]}  {h[5]}")


main()

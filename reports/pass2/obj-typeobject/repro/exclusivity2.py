"""Transitive exclusivity audit for Objects/typeobject.c.

Builds an intra-file call graph, seeds it from every TYPE_LOCK / stop-the-world
region, and reports every user-Python-capable call reachable while exclusivity
is held.
"""
import re
import sys
from collections import defaultdict, deque

PATH = "/home/danzin/projects/cpython/Objects/typeobject.c"
lines = open(PATH, encoding="utf-8").read().splitlines()
n = len(lines)


def strip_comment(s):
    s = re.sub(r"//.*", "", s)
    s = re.sub(r"/\*.*?\*/", "", s)
    return s


code = [strip_comment(x) for x in lines]

# --- function bodies: definition line is `name(...)` at col 0, body starts at
# the next line whose stripped content is '{' or ends with '{'.
funcs = []  # (name, defline, startline, endline) 1-based inclusive
i = 0
while i < n:
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(", code[i])
    if m:
        # locate opening brace within next 12 lines
        j = i
        depth = 0
        open_at = None
        while j < min(i + 14, n):
            if "{" in code[j] and not code[j].lstrip().startswith("#"):
                open_at = j
                break
            if ";" in code[j]:
                break
            j += 1
        if open_at is not None:
            # brace match
            depth = 0
            k = open_at
            endl = None
            while k < n:
                for ch in code[k]:
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            endl = k
                            break
                if endl is not None:
                    break
                k += 1
            if endl is not None and endl > open_at:
                funcs.append((m.group(1), i + 1, open_at + 1, endl + 1))
                i = endl
    i += 1

byname = {}
for nm, d, s, e in funcs:
    byname.setdefault(nm, (nm, d, s, e))


def owner(lineno):
    for nm, d, s, e in funcs:
        if s <= lineno <= e:
            return nm
    return None


CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
KW = {"if", "for", "while", "switch", "return", "sizeof", "assert", "defined"}

callgraph = defaultdict(set)
callsites = defaultdict(list)
for nm, d, s, e in funcs:
    for ln in range(s, e + 1):
        for m in CALL.finditer(code[ln - 1]):
            c = m.group(1)
            if c in KW:
                continue
            callgraph[nm].add(c)
            callsites[(nm, c)].append(ln)

# --- exclusivity regions
regions = []  # (kind, startline, endline, func)
OPENERS = [
    ("TYPE_LOCK", "BEGIN_TYPE_LOCK()", "END_TYPE_LOCK()"),
    ("TYPE_DICT_LOCK", "BEGIN_TYPE_DICT_LOCK(", "END_TYPE_DICT_LOCK()"),
    ("STW", "types_stop_world()", "types_start_world()"),
    ("STW", "_PyEval_StopTheWorld(", "_PyEval_StartTheWorld("),
]
for kind, op, cl in OPENERS:
    for ln in range(1, n + 1):
        t = code[ln - 1]
        if op in t and "#define" not in t and "static void" not in t:
            end = None
            for j in range(ln + 1, min(ln + 500, n + 1)):
                if cl in code[j - 1]:
                    end = j
                    break
            if end:
                regions.append((kind, ln, end, owner(ln)))

# functions whose whole body asserts the lock is held
assert_held = set()
for nm, d, s, e in funcs:
    for ln in range(s, e + 1):
        if "ASSERT_TYPE_LOCK_HELD()" in code[ln - 1]:
            assert_held.add(nm)

# seeds: callees appearing textually inside a region
seeds = defaultdict(set)  # func -> kinds
for kind, s, e, own in regions:
    for ln in range(s, e):
        for m in CALL.finditer(code[ln - 1]):
            c = m.group(1)
            if c in KW:
                continue
            seeds[c].add(kind)
for f in assert_held:
    seeds[f].add("TYPE_LOCK")

# propagate
state = defaultdict(set)
q = deque()
for f, kinds in seeds.items():
    state[f] |= kinds
    q.append(f)
while q:
    f = q.popleft()
    for c in callgraph.get(f, ()):
        if c in byname:
            new = state[f] - state[c]
            if new:
                state[c] |= new
                q.append(c)

USER_CODE = re.compile(
    r"\b(call_method\w*|call_unbound\w*|lookup_maybe_method|lookup_method|"
    r"PyObject_Call\w*|_PyObject_Call\w*|PyObject_RichCompare\w*|"
    r"PyObject_GetAttr\w*|PyObject_SetAttr\w*|PyObject_GetOptionalAttr\w*|"
    r"_PyObject_LookupSpecial\w*|PyObject_Repr|PyObject_Str|PyObject_Hash|"
    r"PyErr_FormatUnraisable|PyErr_WriteUnraisable|PySequence_Tuple|PySequence_List|"
    r"PyIter_Next|PyObject_GetIter|PyObject_IsSubclass|PyObject_IsInstance|"
    r"PyDict_Merge|PyDict_Update|PyObject_GenericGetAttr|PyObject_GenericSetAttr|"
    r"_PyObject_GenericGetAttrWithDict|PyMapping_GetOptionalItem)\b"
)

print("== regions ==")
for kind, s, e, own in sorted(regions, key=lambda r: r[1]):
    print(f"  {kind:14s} {s:6d}-{e:<6d} in {own}")

print("\n== user-Python-capable calls reachable under exclusivity ==")
seen = set()
for nm, d, s, e in funcs:
    if not state.get(nm):
        continue
    for ln in range(s, e + 1):
        for m in USER_CODE.finditer(code[ln - 1]):
            key = (nm, ln, m.group(1))
            if key in seen:
                continue
            seen.add(key)
            print(f"  [{'+'.join(sorted(state[nm])):14s}] {nm:38s} :{ln:<6d} {m.group(1):28s} | {lines[ln-1].strip()[:90]}")

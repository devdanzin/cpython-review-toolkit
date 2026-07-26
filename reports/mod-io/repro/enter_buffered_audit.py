"""Audit ENTER_BUFFERED / LEAVE_BUFFERED balance in Modules/_io/bufferedio.c.

scan_lock_discipline reports 0 findings with mutex_functions:0 on this file --
its vocabulary has no ENTER_BUFFERED.  This walks each function, tracks a
depth counter over the raw text, and reports every `return` reached while the
counter is > 0 (i.e. a path that leaves the locked region without releasing).
"""
import re
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else \
    "/home/danzin/projects/cpython/Modules/_io/bufferedio.c"

lines = open(SRC).read().splitlines()

# find function bodies: a line starting at col 0 with `name(` ... up to a lone `}`
fn_start = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(")
funcs = []
i = 0
while i < len(lines):
    m = fn_start.match(lines[i])
    if m and i + 1 < len(lines) and lines[i + 1].startswith("{"):
        j = i + 1
        depth = 0
        while j < len(lines):
            depth += lines[j].count("{") - lines[j].count("}")
            if depth == 0 and j > i + 1:
                break
            j += 1
        funcs.append((m.group(1), i + 1, j + 1))
        i = j
    i += 1

print("functions parsed: %d" % len(funcs))
issues = 0
enter_fns = 0
for name, a, b in funcs:
    body = lines[a - 1:b]
    if not any("ENTER_BUFFERED" in l for l in body):
        continue
    enter_fns += 1
    held = 0
    labels_held = {}
    # first pass: find which labels are reached with the lock held
    for off, l in enumerate(body):
        s = l.split("/*")[0]
        if "ENTER_BUFFERED(self)" in s and "#define" not in s:
            held += 1
        if "LEAVE_BUFFERED(self)" in s and "#define" not in s:
            held -= 1
        m = re.match(r"^([a-z_][a-z0-9_]*):\s*$", l.strip())
        if m:
            labels_held[m.group(1)] = held
    held = 0
    for off, l in enumerate(body):
        s = l.split("/*")[0]
        ln = a + off
        if "ENTER_BUFFERED(self)" in s and "#define" not in s:
            held += 1
            continue
        if "LEAVE_BUFFERED(self)" in s and "#define" not in s:
            held -= 1
            continue
        m = re.match(r"^([a-z_][a-z0-9_]*):\s*$", l.strip())
        if m:
            held = labels_held.get(m.group(1), held)
            continue
        if held > 0 and re.search(r"\breturn\b", s):
            # `if (!ENTER_BUFFERED(self)) return NULL;` -- the enter FAILED, not held
            prev = body[off - 1] if off else ""
            if "!ENTER_BUFFERED" in s or "!ENTER_BUFFERED" in prev:
                continue
            print("  LEAK? %-38s :%d  held=%d  %s" % (name, ln, held, s.strip()))
            issues += 1

print("functions using ENTER_BUFFERED: %d" % enter_fns)
print("suspect returns inside a held region: %d" % issues)

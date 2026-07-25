#!/usr/bin/env python3
"""Summarize TSan stress logs: dedupe races by their unordered file:func pair."""
import collections
import glob
import os
import re
import sys

LOGDIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tsan-logs")

ACCESS_RE = re.compile(
    r"^\s+(?:Previous )?(Write|Read|Atomic write|Atomic read|Atomic [a-z-]+) "
    r"of size \d+ at 0x[0-9a-f]+ by (?:main )?thread", re.I)
FRAME0_RE = re.compile(r"^\s+#0 (\S+) .*?((?:Objects|Python|Modules|Include)/[\w./]+:\d+)")

grand = collections.Counter()

for path in sorted(glob.glob(os.path.join(LOGDIR, "tsan_stress_*.err"))):
    text = open(path, errors="replace").read()
    blocks = text.split("WARNING: ThreadSanitizer: data race")[1:]
    per = collections.Counter()
    for block in blocks:
        block = block.split("SUMMARY:")[0]
        lines = block.splitlines()
        accesses = []
        i = 0
        while i < len(lines):
            m = ACCESS_RE.match(lines[i])
            if m:
                kind = m.group(1).lower()
                for j in range(i + 1, min(i + 4, len(lines))):
                    f = FRAME0_RE.match(lines[j])
                    if f:
                        accesses.append("%s %s %s" % (kind, f.group(1), f.group(2)))
                        break
            i += 1
        if len(accesses) >= 2:
            key = tuple(sorted(accesses[:2]))
            per[key] += 1
            grand[key] += 1
    name = os.path.basename(path).replace("tsan_stress_", "").replace(".err", "")
    print("--- %-58s %3d races, %d distinct pairs" % (name, len(blocks), len(per)))
    for k, n in per.most_common(12):
        print("      %3dx  %s" % (n, k[0]))
        print("            VS %s" % (k[1],))
    print()

print("=== GRAND TOTAL: %d distinct site-pairs ===" % len(grand))
for k, n in grand.most_common(40):
    print("  %3dx  %s\n        VS %s" % (n, k[0], k[1]))

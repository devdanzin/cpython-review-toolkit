"""Probe: how far can a failing set_table_resize drive so->fill?

    <build>/python set_resize_probe.py START WIDTH

Arms _testcapi.set_nomemory(START, START+WIDTH), then adds pre-interned small
ints to a set.  Exits via os._exit with the resulting len(set) so no allocation
is needed to report.  Exit code 200 means the loop finished normally.
"""

import os
import sys

import _testcapi

start = int(sys.argv[1])
width = int(sys.argv[2]) if len(sys.argv) > 2 else 1

keys = list(range(60))
s = set()
# Warm: run the whole add sequence once on a throwaway set so every code path,
# specialisation and freelist is already hot.
warm = set()
for k in keys:
    warm.add(k)
del warm

n_memerr = 0
_testcapi.set_nomemory(start, start + width)
for k in keys:
    try:
        s.add(k)
    except MemoryError:
        n_memerr += 1

os._exit(n_memerr)

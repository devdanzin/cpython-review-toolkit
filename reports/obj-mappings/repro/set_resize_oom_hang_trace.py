"""Instrumented variant of set_resize_oom_hang.py -- prints one pre-built marker
per add so the hanging iteration is visible.  All bytes objects and the key list
are built BEFORE set_nomemory is armed, and os.write is unbuffered, so the last
marker printed is the last add that returned.
"""

import os
import sys

import _testcapi

keys = list(range(60))
adds = keys[:40]
marks = [("i=%02d " % i).encode() for i in range(40)]
NL = b"\n"
DONE = b"\nadds-done\n"

s = set()
warm = set()
for k in adds:
    warm.add(k)
del warm

_testcapi.set_nomemory(0, 10)
i = 0
for k in adds:
    os.write(1, marks[i])
    try:
        s.add(k)
    except MemoryError:
        os.write(1, b"M ")
    i += 1
os.write(1, DONE)
os._exit(0)

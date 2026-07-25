"""A failed set_table_resize can leave the table with NO virgin slot -> the next
probing loop never terminates (100% CPU hang, GIL held, uninterruptible).

Mechanism (Objects/setobject.c):
  * set_add_entry_takeref `found_unused:` (:319-326) writes the key and bumps
    so->fill / so->used BEFORE it calls set_table_resize().
  * set_table_resize (:527-531) can fail with MemoryError; the caller returns
    -1 but the insertion is already committed.
  * The load-factor test `(size_t)so->fill*5 < mask*3` fires again on the next
    add, which fails again, so fill keeps climbing past the point the resize
    was supposed to prevent.
  * setobject.c:515-520 states the invariant: "set_lookkey needs at least one
    virgin slot to terminate failing searches."  Once fill == mask+1 the
    `while (1)` in set_do_lookup (:230-246) and in set_add_entry_takeref
    (:271-306) never sees an empty entry.

The OOM window is BOUNDED (set_nomemory(0, 10)): by the time the probe runs the
allocator is healthy again, so a hang here is the set's probing loop and not an
allocation-famine livelock.

Run:  timeout 25 <build>/python set_resize_oom_hang.py
  stdout "phase=<n>" markers say how far it got before hanging.
  exit 0    = survived
  exit 124  = HANG reproduced (100% CPU, R state)
"""

import os
import sys

import _testcapi

M_ADDS = b"phase=adds-done\n"
M_ALLOC = b"phase=allocator-healthy\n"
M_PROBE = b"phase=probe-start\n"
M_DONE = b"phase=probe-returned\n"

keys = list(range(60))       # small ints: already interned, no allocation
probe = 44                   # absent from the set after the loop below

adds = keys[:40]             # built BEFORE arming: the loop must not allocate

s = set()
warm = set()                 # warm every code path unarmed
for k in adds:
    warm.add(k)
del warm

_testcapi.set_nomemory(0, 10)   # BOUNDED: 10 allocations fail, then normal
for k in adds:
    try:
        s.add(k)
    except MemoryError:
        pass

os.write(1, M_ADDS)
bytearray(4096)                 # proves the allocator is working again
os.write(1, M_ALLOC)
os.write(1, M_PROBE)
found = probe in s
os.write(1, M_DONE)
os._exit(9 if found else 0)

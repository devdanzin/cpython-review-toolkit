"""Does a failed set_table_resize leave the table with no virgin slot?

Objects/setobject.c:319-326 (set_add_entry_takeref, `found_unused:`) inserts the
key and bumps so->fill/so->used BEFORE calling set_table_resize().  If the
resize allocation fails (setobject.c:527-531 -> PyErr_NoMemory, return -1) the
insertion is already committed, so `set.add()` raises MemoryError with the
element in the set and the table one slot fuller.

setobject.c:515-520 states the invariant this threatens:

    Subtle:  This is *necessary* if fill==size, as set_lookkey needs at least
    one virgin slot to terminate failing searches.

If fill can be driven to mask+1 this way, set_do_lookup()'s `while (1)` at
setobject.c:230-246 never sees an empty entry and a lookup for an ABSENT key
does not terminate.

Run:  timeout 20 <build>/python set_resize_oom_full_table.py
"""

import sys

import _testcapi

# Pre-create everything the loop needs so that the armed window contains no
# allocation but the resize itself.
keys = list(range(200))
probe = 10_000_000  # absent from the set; small ints are already interned
s = set()
s.add(keys[0])
s.discard(keys[0])  # force the smalltable into use, warm the code path

report = []

_testcapi.set_nomemory(0)  # unbounded: every allocation from now on fails
try:
    for k in keys:
        try:
            s.add(k)
        except MemoryError:
            report.append(k)
        if len(s) >= 40:
            break
finally:
    pass

# Cannot un-arm set_nomemory, so report through the exit code instead of I/O.
n_used = len(s)
n_memerr = len(report)

# The dangerous probe: a lookup for a key that is NOT present.  If the table has
# no virgin slot this never returns.
found = probe in s

# Exit code carries the result: 0 = survived, non-zero would be a crash.
sys.exit(0 if not found else 9)

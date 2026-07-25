"""dict_merge (dictobject.c:4300) slow path: does a re-entrant keys() recurse
unboundedly in C?

The slow path runs PyMapping_Keys(b) and PyObject_GetItem(b, key) -- arbitrary
Python -- inside Py_BEGIN_CRITICAL_SECTION(a).  A mapping whose keys() calls
d.update(self) again re-enters dict_merge.  Every level goes through a Python
frame, so the eval loop's Py_EnterRecursiveCall bounds it; this probe checks
that the C frames dict_merge itself adds do not outrun that bound.

Expected: RecursionError, not SIGSEGV.
"""

import sys

sys.setrecursionlimit(1_000_000)

d = {}


class Reenter:
    def keys(self):
        d.update(self)          # re-enter dict_merge on the same dict
        return ["k"]

    def __getitem__(self, k):
        return 1


try:
    d.update(Reenter())
except RecursionError as e:
    print("RecursionError:", e, flush=True)
print("DONE", flush=True)

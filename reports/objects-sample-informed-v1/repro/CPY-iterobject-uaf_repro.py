"""Independent reproducer for the claimed iterobject.c:80 double-DECREF.

iter_iternext() loads `seq = it->it_seq` (BORROWED, line 61), then calls
PySequence_GetItem(seq, ...) (line 70) which can run arbitrary Python. If that
Python re-enters next() on the SAME iterator and the inner call takes the
IndexError/StopIteration branch, the inner frame does:
    it->it_seq = NULL; Py_DECREF(seq);
dropping the one reference. The outer frame's `seq` local is now stale, and it
executes the very same Py_DECREF(seq) again -> double-DECREF -> UAF.

Guarded twin: calliter_iternext (same file, ~165 lines below) uses
Py_CLEAR(it->it_callable), which NULLs-and-decrefs from the struct atomically
instead of dropping a stale local.
"""

import sys


class Seq:
    """A sequence whose __getitem__ re-enters next() on its own iterator."""

    def __init__(self):
        self.calls = 0

    def __getitem__(self, index):
        self.calls += 1
        if self.calls == 1:
            # Re-enter the iterator from inside the outer PySequence_GetItem.
            # The inner iter_iternext() takes the IndexError branch, sets
            # it_seq = NULL and drops the reference.
            try:
                next(it)
            except StopIteration:
                pass
            # Now make the OUTER call fail the same way, so it repeats the
            # Py_DECREF on its stale `seq` local.
            raise IndexError(index)
        raise IndexError(index)


s = Seq()
it = iter(s)
print("refcount(s) before:", sys.getrefcount(s), flush=True)

try:
    next(it)
except StopIteration:
    pass

# If the double-DECREF happened, s has lost a reference it never gave up.
print("refcount(s) after: ", sys.getrefcount(s), flush=True)
print("calls:", s.calls, flush=True)

# Force the issue: touch the object repeatedly to surface a freed/poisoned slot.
for _ in range(3):
    s.calls += 1
print("survived; final calls:", s.calls, flush=True)

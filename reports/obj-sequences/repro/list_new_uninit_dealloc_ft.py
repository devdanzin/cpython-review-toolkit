#!/usr/bin/env python3
"""CPY-0014 confirmation attempt -- PyList_New's free-threaded branch.

    Objects/listobject.c:250   op = PyObject_GC_New(PyListObject, &PyList_Type);
    Objects/listobject.c:259       _PyListArray *array = list_allocate_array(size);
    Objects/listobject.c:260       if (array == NULL) {
    Objects/listobject.c:261           Py_DECREF(op);          <- ob_item / ob_size still garbage
    Objects/listobject.c:262           return PyErr_NoMemory();
    Objects/listobject.c:567   list_dealloc: if (op->ob_item != NULL) { i = Py_SIZE(op); ... }

`list_dealloc` opens with `PyObject_GC_UnTrack` -- the untracked-TOLERANT
function, not the unchecked `_PyObject_GC_UNTRACK` macro -- so this shape is
latent, not deterministic (the odictiter_new / CPY-0011 contrast).  It also
guards on `op->ob_item != NULL`, so it only faults when the recycled block
happens to carry non-NULL garbage at that offset.

The block therefore has to be DIRTY.  Two things make it clean by default:

  * `_Py_FREELIST_POP(PyListObject, lists)` runs first and a freelist entry was
    NULLed by `list_dealloc:569` before being pushed, so the freelist path can
    never crash.  The freelist must be drained (hold the lists alive).
  * a same-size-class pymalloc block that last held a list is clean for the
    same reason.  The pool has to be dirtied with a DIFFERENT object that
    leaves a non-NULL pointer at offset 24.

This script does both, then sweeps `_testcapi.set_nomemory` over the
`list_allocate_array` failure.  Free-threaded builds only -- the branch is
inside `#ifdef Py_GIL_DISABLED`.
"""

import subprocess
import sys

CHILD = r'''
import sys, faulthandler
faulthandler.enable()
import _testcapi

N = {n}
SIZE = {size}

# (1) Drain the PyListObject freelist by holding its entries alive.  A freelist
#     block was NULLed by list_dealloc:569 and can never carry garbage.
keep = [[] for _ in range(4000)]

# (2) Dirty the 64-byte pymalloc size class with objects that leave a non-NULL
#     pointer where PyListObject::ob_item lives (offset 24: a 2-tuple's
#     ob_item[0], a 1-element list's inline-ish payload, a small dict, ...).
sentinel = object()
churn = [(sentinel, sentinel) for _ in range(20000)]
del churn
churn = [{{sentinel: sentinel}} for _ in range(4000)]
del churn

# Warm the constructor unarmed.
probe = [None] * SIZE
del probe

_testcapi.set_nomemory(N, N + 1)
try:
    x = [None] * SIZE
    r = "no-exception"
except MemoryError:
    r = "MemoryError"
except BaseException as e:
    r = "%s: %s" % (type(e).__name__, e)
finally:
    try:
        _testcapi.remove_mem_hooks()
    except Exception:
        pass
print("OP:%s" % r)
sys.stdout.flush()
import gc
gc.collect()
print("DONE")
'''


def main():
    python = sys.argv[1]
    size = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    hi = int(sys.argv[3]) if len(sys.argv) > 3 else 40
    crashes = 0
    for n in range(hi):
        p = subprocess.run(
            [python, "-c", CHILD.format(n=n, size=size)],
            capture_output=True, text=True, timeout=300,
        )
        if p.returncode != 0 or "Sanitizer" in p.stderr:
            crashes += 1
            print(f"n={n:3d} rc={p.returncode} {p.stdout.strip()!r}")
            print("   " + "\n   ".join(p.stderr.strip().splitlines()[:10]))
    print(f"== {hi} indices, {crashes} crashes")


if __name__ == "__main__":
    main()

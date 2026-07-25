"""CPY-0014 reached from Objects/typeobject.c -- free-threaded builds only.

mro_implementation_unlocked (Objects/typeobject.c:3503) calls PyList_New(1).
On a Py_GIL_DISABLED build PyList_New's list_allocate_array failure branch
Py_DECREFs a list whose ob_item / ob_size were never written, and list_dealloc
frees the uninitialised pointer.  Reached by *creating a class with two bases*
and by *assigning to __bases__* -- no marshal, no C extension.

    python repro_cpy0014_typeobject.py [n]      # n defaults to 3

PyList_New pops a recycled (clean) list from the freelist when one is
available, so the freelist has to be drained first or the bug hides.
"""

import sys

import _testcapi

N = int(sys.argv[1]) if len(sys.argv) > 1 else 3


class M1:
    pass


class M2:
    pass


class Warm(M1, M2):  # warm every lazy path unarmed
    pass


Warm.__bases__ = (M2, M1)

# Drain the list freelist so PyList_New must go to PyObject_GC_New, which
# returns storage with ob_item/ob_size still uninitialised.
_hold = [[] for _ in range(2000)]

import faulthandler

faulthandler.enable()
_testcapi.set_nomemory(N, N + 1)
try:

    class Victim(M1, M2):
        pass

except MemoryError:
    print("MemoryError")
finally:
    _testcapi.remove_mem_hooks()
print("survived")

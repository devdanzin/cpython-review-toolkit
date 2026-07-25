"""`obj.__dict__ = {...}` trips a failable assert on the allocation-failure path.

Objects/dictobject.c:7927-7933 (_PyObject_SetManagedDict, the !Py_GIL_DISABLED arm):

    if (_PyDict_DetachFromObject(dict, obj) == 0) {
        _PyObject_ManagedDictPointer(obj)->dict = (PyDictObject *)Py_XNewRef(new_dict);
        Py_DECREF(dict);
        return 0;
    }
    assert(new_dict == NULL);
    return -1;

_PyDict_DetachFromObject -> detach_dict_from_object (:7953) fails only one way:
copy_values() at :7969 returns NULL and :7973 raises MemoryError.  That has
nothing to do with whether the CALLER passed a new dict, so the assert states an
invariant that does not hold: `obj.__dict__ = {...}` reaches it with
new_dict != NULL.

Debug builds: SIGABRT (Fatal Python error: Aborted).
Release builds: the assert is compiled out and the function correctly returns -1
with MemoryError set, so the bug is the assertion, not the control flow.

    <build>/python CPY-setmanageddict_assert.py [N]
"""

import sys
import faulthandler

import _testcapi

n = int(sys.argv[1]) if len(sys.argv) > 1 else 0


class Inst:
    def __init__(self):
        self.a = 1
        self.b = 2
        self.c = 3


pool = [Inst() for _ in range(64)]
replacement = {"a": 1}
counter = [0]


def exercise():
    inst = pool[counter[0]]
    counter[0] += 1
    inst.__dict__            # materialise the dict onto the inline values
    inst.__dict__ = replacement


for _ in range(8):           # warm unarmed
    exercise()

faulthandler.enable()
_testcapi.set_nomemory(n, n + 1)
try:
    exercise()
except MemoryError:
    print("clean MemoryError at n=%d" % n)
else:
    print("completed at n=%d" % n)

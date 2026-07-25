"""list.remove() deletes the WRONG element after a mutating __eq__.

Objects/listobject.c:3403-3424 list_remove_impl

    for (i = 0; i < Py_SIZE(self); i++) {
        PyObject *obj = self->ob_item[i];
        Py_INCREF(obj);
        int cmp = PyObject_RichCompareBool(obj, value, Py_EQ);   // :3412 runs Python
        Py_DECREF(obj);
        if (cmp > 0) {
            if (list_ass_slice_lock_held(self, i, i+1, NULL) == 0)  // :3415 STALE i
                Py_RETURN_NONE;

`i` is captured before :3412 and used at :3415.  The guarded twin
Modules/_collectionsmodule.c:1477-1481 (deque_remove_impl) snapshots
deque->state before the loop and raises IndexError if the comparison mutated
the deque.  list has no such counter.

Memory-safe: list_ass_slice_lock_held clamps ilow/ihigh to [0, Py_SIZE].
The defect is silent data corruption, not a crash.
"""

import sys

TARGET = "target"

# --- case 1: __eq__ prepends, so index i now names a different element -------
log1 = []


class ShiftLeft:
    def __eq__(self, other):
        if not log1:
            log1.append(1)
            lst1.insert(0, "INSERTED")
        return True

    def __repr__(self):
        return "ShiftLeft"


lst1 = ["a", "b", ShiftLeft(), "d", "e"]
before1 = list(lst1)
lst1.remove(TARGET)
print("case1 before : %r" % (before1,))
print("case1 after  : %r" % (lst1,))
print("case1 verdict: %s" % (
    "WRONG ELEMENT REMOVED (ShiftLeft survives, 'b' is gone)"
    if any(isinstance(x, ShiftLeft) for x in lst1) else "correct"))

# --- case 2: __eq__ clears the list -> i is past the end --------------------
log2 = []


class ClearAll:
    def __eq__(self, other):
        if not log2:
            log2.append(1)
            lst2.clear()
        return True

    def __repr__(self):
        return "ClearAll"


lst2 = ["a", "b", ClearAll()]
try:
    lst2.remove(TARGET)
    print("case2 after  : %r (no crash: list_ass_slice clamps)" % (lst2,))
except Exception as exc:
    print("case2 raised : %r" % (exc,))

# --- case 3: __eq__ shrinks the list to exactly i ---------------------------
log3 = []


class Shrink:
    def __eq__(self, other):
        if not log3:
            log3.append(1)
            del lst3[1:]
        return True

    def __repr__(self):
        return "Shrink"


lst3 = ["a", "b", "c", Shrink()]
try:
    lst3.remove(TARGET)
    print("case3 after  : %r" % (lst3,))
except Exception as exc:
    print("case3 raised : %r" % (exc,))

print("SURVIVED", flush=True)
sys.exit(0)

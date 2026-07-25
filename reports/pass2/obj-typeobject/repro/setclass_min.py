"""MINIMAL: over-DECREF of a type object via re-entrant __class__ assignment.

Objects/typeobject.c:7799 object_set_class

    int unique = _PyObject_IsUniquelyReferenced(self);
    if (!unique) types_stop_world();
    PyTypeObject *oldto = Py_TYPE(self);            <-- captured here
    int res = object_set_class_world_stopped(self, newto);
    if (!unique) types_start_world();
    if (res == 0) {
        if (oldto->tp_flags & Py_TPFLAGS_HEAPTYPE) Py_DECREF(oldto);   <-- STALE

object_set_class_world_stopped -> compatible_for_assignment ->
same_slots_added (:7609) -> PyObject_RichCompareBool(ht_slots_a, ht_slots_b)
runs a user __eq__ on a str-subclass __slots__ name.  If that __eq__ reassigns
self.__class__, the inner call already dropped the entry type's reference and
installed its own; the outer call then Py_SET_TYPE()s over the intermediate
(leak) and Py_DECREF()s the stale oldto a second time.

release-gil-nojit      -> SIGSEGV
debug-gil-nojit        -> Assertion 'object has negative ref count'
release-gil-nojit-asan -> heap-use-after-free READ on the type object
debug-ft-nojit         -> Assertion '!interp->stoptheworld.world_stopped'
release-ft-nojit       -> hang (nested _PyEval_StopTheWorld)
"""
import sys


armed = True


class S(str):
    def __eq__(self, other):
        global armed
        if armed:                # one-shot: unbounded re-entry would just
            armed = False        # RecursionError inside CPY-0078's swallow
            o.__class__ = C      # re-entrant, from inside same_slots_added
        return True
    __hash__ = str.__hash__


A = type("A", (), {"__slots__": (S("x"),)})
B = type("B", (), {"__slots__": (S("x"),)})
C = type("C", (), {"__slots__": (S("x"),)})

o = A()
keep = o
print("A refcnt before:", sys.getrefcount(A), " C refcnt before:", sys.getrefcount(C))
o.__class__ = B
print("A refcnt after :", sys.getrefcount(A), " C refcnt after :", sys.getrefcount(C))
print("expected: A -1, C unchanged;  actual: A -2 (over-decref), C +1 (leak)")

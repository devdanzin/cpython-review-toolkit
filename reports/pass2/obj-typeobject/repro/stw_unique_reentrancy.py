"""Probe 3: re-entrancy on the NON-stop-the-world fast path of object_set_class.

gh-145566 (1d091a336e60, 2026-03-06) made object_set_class (typeobject.c:7800)
skip stop-the-world when _PyObject_IsUniquelyReferenced(self) holds:

    int unique = _PyObject_IsUniquelyReferenced(self);   // :7821
    if (!unique) { types_stop_world(); }                 // :7822-7824
    PyTypeObject *oldto = Py_TYPE(self);                 // :7825  BORROWED
    int res = object_set_class_world_stopped(self, newto);
    if (!unique) { types_start_world(); }                // :7827-7829
    if (res == 0) {
        if (oldto->tp_flags & Py_TPFLAGS_HEAPTYPE) {
            Py_DECREF(oldto);                            // :7832
        }

object_set_class_world_stopped -> compatible_for_assignment -> same_slots_added
-> PyObject_RichCompareBool(ht_slots_a, ht_slots_b) -> __eq__ of a str subclass
stored in __slots__ -> arbitrary Python.  On the !unique path that Python cannot
run (the world is stopped and a nested stop deadlocks); on the unique path it
runs freely and can re-enter object_set_class on the very same object through a
weakref, which retypes `self` and drops the reference `oldto` still names.

Expected: `oldto` is decremented twice (once by the inner call, once at :7832)
and the class installed by the inner call leaks.
"""
import gc
import sys
import weakref

wr = None
fired = []


class MyStr(str):
    def __eq__(self, other):
        if wr is not None and not fired:
            fired.append(1)
            o = wr()          # strong ref, obtained without ever bumping ob_ref_shared before :7821
            if o is not None:
                print("  [__eq__ re-enters: setting __class__ =", C.__name__, "]", flush=True)
                o.__class__ = C
                print("  [__eq__ inner assignment done, type(o) =", type(o).__name__, "]", flush=True)
                del o
        return str.__eq__(self, other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return str.__hash__(self)

    def __lt__(self, other):
        return str.__lt__(self, other)


class Base:
    pass


class A(Base):
    __slots__ = (MyStr("x"),)


class B(Base):
    __slots__ = (MyStr("x"),)


class C(Base):
    __slots__ = (MyStr("x"),)


def refs(t):
    return sys.getrefcount(t)


a = A()
wr = weakref.ref(a)
print("A refcount before:", refs(A), " B:", refs(B), " C:", refs(C), flush=True)
print("outer: a.__class__ = B", flush=True)
a.__class__ = B
print("outer done, type(a) =", type(a).__name__, flush=True)
print("A refcount after :", refs(A), " B:", refs(B), " C:", refs(C), flush=True)
del a
gc.collect()
print("after del a + gc:  A:", refs(A), " B:", refs(B), " C:", refs(C), flush=True)
print("touching A to see if it survived:", A.__name__, A.__mro__, flush=True)
for i in range(3):
    gc.collect()
print("still alive after 3 gc passes:", A.__name__, flush=True)

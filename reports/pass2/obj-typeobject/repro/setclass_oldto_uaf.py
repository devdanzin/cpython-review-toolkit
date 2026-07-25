"""Minimal reproducer: object_set_class over-decrements `oldto`.

Objects/typeobject.c:7825   PyTypeObject *oldto = Py_TYPE(self);      <- borrowed
Objects/typeobject.c:7826   int res = object_set_class_world_stopped(self, newto);
Objects/typeobject.c:7832       Py_DECREF(oldto);

object_set_class_world_stopped -> compatible_for_assignment (:7763)
  -> same_slots_added (:7588) -> PyObject_RichCompareBool (:7609)
  -> __eq__ of a str subclass stored in __slots__  == arbitrary Python.

That Python re-enters object_set_class on the same object, which drops the
reference `oldto` names.  The outer frame then drops it a second time.

Before gh-120198 (3bfc9c831ad9, 2024-07-11) the line right above Py_SET_TYPE was
    // The real Py_TYPE(self) (`oldto`) may have changed from
    // underneath us in another thread, so we re-fetch it here.
    oldto = Py_TYPE(self);
That re-fetch was deleted; nothing replaced it on the default (GIL) build, where
types_stop_world() is an empty macro.
"""
import gc
import sys

armed = [True]


class MyStr(str):
    def __eq__(self, other):
        if armed[0]:
            armed[0] = False
            obj.__class__ = C          # re-entrant retype; drops obj's ref to A
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


obj = A()
before = (sys.getrefcount(A), sys.getrefcount(B), sys.getrefcount(C))
obj.__class__ = B
after = (sys.getrefcount(A), sys.getrefcount(B), sys.getrefcount(C))
print("refcount delta (A, B, C) =", tuple(x - y for x, y in zip(after, before)))
print("expected            =  (-1, +1, 0)   [obj retyped A -> ... -> B]")
print("type(obj) =", type(obj).__name__)
for _ in range(4):
    gc.collect()
print("survived gc; A =", A.__name__, A.__mro__)

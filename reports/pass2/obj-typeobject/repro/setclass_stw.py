"""Minimal: object.__class__ assignment runs arbitrary Python with the world stopped.

No re-entrancy needed.  Objects/typeobject.c:7821-7829
    if (!unique) types_stop_world();
    res = object_set_class_world_stopped(self, newto);
    if (!unique) types_start_world();
reaches same_slots_added() -> PyObject_RichCompareBool(ht_slots_a, ht_slots_b)
-> a user __eq__ on a str-subclass __slots__ name, executed while
_PyEval_StopTheWorld() is in effect.
"""
import sys


class S(str):
    def __eq__(self, other):
        print("  [S.__eq__ ran; world is supposed to be stopped]", flush=True)
        return True

    def __hash__(self):
        return str.__hash__(self)


A = type("A", (), {"__slots__": (S("x"),)})
B = type("B", (), {"__slots__": (S("x"),)})

o = A()
keep = o          # make refcount > 1 so _PyObject_IsUniquelyReferenced() is false
print("gil disabled:", getattr(sys, "_is_gil_enabled", lambda: True)() is False, flush=True)
print("assigning ...", flush=True)
o.__class__ = B
print("done, type =", type(o).__name__, flush=True)

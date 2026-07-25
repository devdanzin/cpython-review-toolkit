"""Re-entrant object.__class__ assignment via a hostile __slots__ name __eq__.

Objects/typeobject.c:7799 object_set_class captures `oldto = Py_TYPE(self)`
BEFORE calling object_set_class_world_stopped(), which reaches
same_slots_added() -> PyObject_RichCompareBool(ht_slots_a, ht_slots_b, Py_EQ)
-> arbitrary Python. If that Python re-assigns self.__class__, the inner call
already released the entry type's reference; the outer call then
  (a) Py_SET_TYPE(self, newto) dropping the intermediate type's reference
      with no DECREF  -> leak, and
  (b) Py_DECREF(oldto) a second time -> over-decref of the entry type.
"""

import sys

arm = False
victim = None
inner_target = None
n_eq = 0
n_reentered = 0


class S(str):
    def __eq__(self, other):
        global arm, n_eq, n_reentered
        n_eq += 1
        if arm:
            arm = False
            n_reentered += 1
            victim.__class__ = inner_target
        return True

    def __ne__(self, other):
        return False

    def __lt__(self, other):
        return str.__lt__(self, other)

    def __hash__(self):
        return str.__hash__(self)


def mk(name):
    # Distinct S instances so the identity shortcut in PyObject_RichCompareBool
    # does not fire and S.__eq__ actually runs.
    return type(name, (), {"__slots__": (S("x"),)})


A = mk("A")
B = mk("B")
C = mk("C")

print("basicsizes", A.__basicsize__, B.__basicsize__, C.__basicsize__, flush=True)

victim = A()
inner_target = C

# baseline: does a plain (non-reentrant) assignment even reach S.__eq__?
before = n_eq
victim.__class__ = B
print("plain assign reached __eq__:", n_eq > before, "-> type now", type(victim).__name__, flush=True)
victim.__class__ = A

keep = [A, B, C]
print("refcounts A/B/C before:", sys.getrefcount(A), sys.getrefcount(B), sys.getrefcount(C), flush=True)

arm = True
victim.__class__ = B
print("reentered:", n_reentered, "final type:", type(victim).__name__, flush=True)
print("refcounts A/B/C after :", sys.getrefcount(A), sys.getrefcount(B), sys.getrefcount(C), flush=True)

# Burn A's refcount down.  Each armed round costs A exactly one reference.
rounds = 0
try:
    for i in range(200):
        victim.__class__ = A
        arm = True
        victim.__class__ = B
        rounds += 1
except BaseException as e:
    print("stopped at round", rounds, type(e).__name__, e, flush=True)

print("rounds", rounds, "reentered", n_reentered, flush=True)
try:
    print("A refcount now", sys.getrefcount(A), flush=True)
except BaseException as e:
    print("getrefcount(A) blew up:", type(e).__name__, e, flush=True)

# Touch A hard: if it was freed while `keep` still points at it this is a UAF.
print("touching A ...", flush=True)
print(A.__name__, A.__mro__, A.__basicsize__, flush=True)
a2 = A()
print("made instance", a2, flush=True)
import gc
gc.collect()
print("OK-END", flush=True)

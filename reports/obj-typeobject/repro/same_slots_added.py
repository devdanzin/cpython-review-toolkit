"""same_slots_added collapses PyObject_RichCompareBool's tri-state.

Objects/typeobject.c:7609

    if (PyObject_RichCompareBool(slots_a, slots_b, Py_EQ) != 1)
        return 0;

PyObject_RichCompareBool returns -1 on error, 0 for false, 1 for true. The
`!= 1` folds -1 and 0 into the same answer, so an exception raised while
comparing the two ht_slots tuples is left pending and the caller reports a
layout mismatch instead.

Guarded twin: Objects/typeobject.c:10566 tests the same call's result for < 0
separately before treating 0 as false.

First step is a feasibility check: __slots__ names are mangled and stored in
ht_slots, and if that conversion produces exact str objects then a str subclass
never reaches the comparison and the site is unreachable from Python. That
result is reported either way -- an unreachable site is a real answer.
"""

import warnings

warnings.simplefilter("ignore")

CALLS = []
ARMED = False


class EvilName(str):
    """A slot name whose __eq__ raises once armed."""

    def __hash__(self):
        return str.__hash__(self)

    def __eq__(self, other):
        CALLS.append(other)
        if ARMED:
            raise KeyboardInterrupt("EXC-FROM-SLOTNAME-EQ")
        return str.__eq__(self, other)


class A:
    __slots__ = (EvilName("x"),)


class B:
    __slots__ = (EvilName("x"),)


slots_a = A.__slots__
stored_a = type(A).__dict__ and A.__dict__.get("__slots__")
print(f"declared __slots__ element type: {type(slots_a[0]).__name__}")

# ht_slots is not directly exposed; the observable proxy is whether the element
# kept its subclass identity after type creation.
kept = [type(s).__name__ for s in A.__slots__]
print(f"after type creation, element types: {kept}")
if kept == ["str"]:
    print(
        "RESULT UNREACHABLE-FROM-PYTHON: __slots__ names are normalised to exact "
        "str, so no user __eq__ can run inside same_slots_added. The tri-state "
        "collapse is real in the source but not Python-reachable by this route."
    )
else:
    ARMED = True
    a = A()
    try:
        a.__class__ = B
    except KeyboardInterrupt as exc:
        print(f"RESULT PROPAGATED: {exc}")
    except BaseException as exc:  # noqa: BLE001
        ctx = type(exc.__context__).__name__
        print(
            f"RESULT REPLACED by {type(exc).__name__}: {exc}"
            f"  __context__={ctx}  (the original exception is lost)"
        )
    else:
        print("RESULT SWALLOWED: assignment succeeded")
print(f"total EvilName.__eq__ calls: {len(CALLS)}")

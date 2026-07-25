"""set.remove() / set.discard() swallow a user __eq__ TypeError.

Objects/setobject.c:2656-2660 (set_remove_impl) and :2696-2700 (set_discard_impl):

    rv = set_discard_key(so, key);
    if (rv < 0) {
        if (!PySet_Check(key) || !PyErr_ExceptionMatches(PyExc_TypeError))
            return NULL;
        PyErr_Clear();
        ... hash = frozenset_hash_impl(key); retry ...

set_discard_key() returns -1 for TWO different reasons:
  (a) PyObject_Hash(key) failed  -- the case the narrowing is written for;
  (b) set_lookkey -> PyObject_RichCompareBool ran a user __eq__ that raised.

The `PySet_Check(key) && ExceptionMatches(TypeError)` test cannot tell them
apart, so a TypeError raised by a colliding element's __eq__ is cleared.

Guarded twins in the same file: _PySet_Contains:2559 and
frozenset___contains___impl:2619 call PyObject_Hash THEMSELVES and clear only
when *that* returned -1, so no comparison has run inside their clear window.

Reachability: needs a set SUBCLASS with a __hash__ (PySet_Check accepts
subclasses) whose hash collides with an element whose __eq__ raises TypeError.

Expected (correct):  TypeError('boom from __eq__') from both calls.
Actual:              remove()  -> KeyError
                     discard() -> silent success, exception gone entirely.
"""

import sys

FAILURES = []


class Boom:
    def __hash__(self):
        return 5

    def __eq__(self, other):
        raise TypeError("boom from __eq__")


class HSet(set):
    # PySet_Check() accepts subclasses; giving it a __hash__ lets the key reach
    # the comparison instead of failing at PyObject_Hash.
    def __hash__(self):
        return 5


def check(label, fn, expect_type, expect_msg):
    try:
        fn()
    except BaseException as e:
        got = type(e).__name__
        if isinstance(e, expect_type) and expect_msg in str(e):
            print(f"  {label}: OK -- {got}: {e}")
            return True
        print(f"  {label}: CLOBBERED -- got {got}: {e!s}")
        FAILURES.append(label)
        return False
    print(f"  {label}: CLOBBERED -- no exception at all (silent success)")
    FAILURES.append(label)
    return False


print("baseline: a direct comparison must raise the user TypeError")
s0 = {Boom()}
check("`Boom() in s`", lambda: Boom() in s0, TypeError, "boom from __eq__")

print("set.remove / set.discard with a colliding set-subclass key")
s1 = {Boom()}
check("s.remove(HSet())", lambda: s1.remove(HSet()), TypeError, "boom from __eq__")
s2 = {Boom()}
check("s.discard(HSet())", lambda: s2.discard(HSet()), TypeError, "boom from __eq__")

print("variant B: a set subclass whose OWN __hash__ raises TypeError")


class HashRaises(set):
    def __hash__(self):
        raise TypeError("my __hash__ says no")


class HashRaisesKBI(set):
    def __hash__(self):
        raise KeyboardInterrupt("not a TypeError")


s3 = {1, 2, 3}
check("s.remove(HashRaises())", lambda: s3.remove(HashRaises()), TypeError, "says no")
check("s.discard(HashRaises())", lambda: s3.discard(HashRaises()), TypeError, "says no")
check("HashRaises() in s", lambda: HashRaises() in s3, TypeError, "says no")
# The narrowing DOES bound the damage: a non-TypeError propagates untouched.
check(
    "s.remove(HashRaisesKBI())  [must NOT be swallowed]",
    lambda: s3.remove(HashRaisesKBI()),
    KeyboardInterrupt,
    "not a TypeError",
)

print()
if FAILURES:
    print("CLOBBER REPRODUCED for: " + ", ".join(FAILURES))
    sys.exit(1)
print("no clobber")
sys.exit(0)

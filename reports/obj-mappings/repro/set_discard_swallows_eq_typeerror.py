"""
setobject.c:2660 (set_discard_impl) / :2700 -- wait, :2660 is remove, :2700 is discard.

set.remove() / set.discard() bundle the hash step and the lookup step into
set_discard_key(), so its -1 is ambiguous: it can mean "key is an unhashable
set" (the case the PyErr_Clear() is for) OR "an element already in the set
raised TypeError from its __eq__ during the probe".

The narrowing `PySet_Check(key) && PyErr_ExceptionMatches(PyExc_TypeError)`
does not separate those, so a set SUBCLASS that defines __hash__ reaches the
clear with a live user exception.

Guarded twin: _PySet_Contains (setobject.c:2559-2572) calls PyObject_Hash
FIRST and only clears when *that* returned -1, so the lookup's comparison
error can never reach its PyErr_Clear().

Expected (correct) behaviour: TypeError("boom from __eq__") propagates.
Observed: the exception is discarded and the call returns normally.
"""

import sys

FAILURES = []


class Bad:
    """Hashes into the same bucket as our probe key, raises from __eq__."""

    def __hash__(self):
        return 1

    def __eq__(self, other):
        raise TypeError("boom from __eq__")


class HashableSet(set):
    """A set SUBCLASS -- PySet_Check() is true -- that IS hashable."""

    def __hash__(self):
        return 1


def check(name, fn):
    s = {Bad()}
    probe = HashableSet()
    try:
        result = fn(s, probe)
    except TypeError as exc:
        if "boom from __eq__" in str(exc):
            print(f"  {name}: OK -- user TypeError propagated")
            return True
        print(f"  {name}: FAIL -- wrong TypeError: {exc!r}")
        FAILURES.append(name)
        return False
    except BaseException as exc:  # noqa: BLE001
        print(f"  {name}: FAIL -- wrong exception: {exc!r}")
        FAILURES.append(name)
        return False
    print(f"  {name}: FAIL -- exception SWALLOWED, returned {result!r}")
    FAILURES.append(name)
    return False


def main():
    print(sys.version)

    # Sanity: the comparison error DOES propagate through the guarded twin.
    s = {Bad()}
    try:
        HashableSet() in s
    except TypeError as exc:
        print(f"  baseline `in` (guarded twin _PySet_Contains): "
              f"propagates {exc!r}")
    else:
        print("  baseline `in`: SWALLOWED (unexpected)")

    print("set.discard / set.remove:")
    check("set.discard", lambda s, k: s.discard(k))
    check("set.remove", lambda s, k: s.remove(k))

    print()
    if FAILURES:
        print(f"RESULT: exception swallowed in {len(FAILURES)} site(s): "
              f"{', '.join(FAILURES)}")
        return 1
    print("RESULT: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())

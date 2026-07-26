"""Error-path agent, slice obj-sequences: gh-148268 (OPEN, in-slice).

Objects/listobject.c:2833 / :2858 / :2873 --

    assert(res == PyObject_RichCompareBool(v, w, Py_LT));

in `unsafe_latin_compare` / `unsafe_long_compare` / `unsafe_float_compare`.
`PyObject_RichCompareBool` returns -1 on failure with an exception set; the
assert compares against a 0/1 `res`, so a failure both fires the assert (debug)
and leaves a live exception behind that nothing consumes.

For exact str / compact int / float operands the only way the call can fail is
`_Py_EnterRecursiveCallTstate` inside `PyObject_RichCompare` -- i.e. the native
C stack must be within the margin at the moment the assert runs, but not so
close that getting into `list.sort` already raised.  This probe sweeps
recursion depths looking for that window.

Usage:  <python> errpath_sort_assert_richcompare.py [depth]
"""

import sys


class Deep:
    __slots__ = ("n",)

    def __init__(self, n):
        self.n = n

    def __lt__(self, other):
        if self.n > 0:
            sorted([Deep(self.n - 1), Deep(self.n - 1)])
        else:
            # exact-latin homogeneous list -> unsafe_latin_compare
            sorted(["aa", "ab"])
            # compact ints -> unsafe_long_compare
            sorted([2, 1])
            # floats -> unsafe_float_compare
            sorted([2.0, 1.0])
        return True


def main(argv):
    if len(argv) > 1:
        depths = [int(argv[1])]
    else:
        depths = [50, 100, 200, 400, 800, 1600, 3200]
    for d in depths:
        try:
            sorted([Deep(d), Deep(d)])
        except RecursionError:
            print(f"PROBE:depth={d} RecursionError (clean)", flush=True)
        except BaseException as exc:  # noqa: BLE001 - probe
            print(f"PROBE:depth={d} {type(exc).__name__}", flush=True)
        else:
            print(f"PROBE:depth={d} ok", flush=True)
    return 0


if __name__ == "__main__":
    sys.setrecursionlimit(100000)
    sys.exit(main(sys.argv))

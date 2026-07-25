"""Exercise the pre-track free path in setobject.c's make_new_set_untracked.

make_new_set_untracked (Objects/setobject.c:1351) allocates with
_PyType_AllocNoTrack, fills from `iterable`, and on failure does
`Py_DECREF(so)` at :1372 -- BEFORE the caller (make_new_set:1383) runs
_PyObject_GC_TRACK.  So set_dealloc runs on a never-GC-tracked object.

set_dealloc (:742) untracks with the *function* form PyObject_GC_UnTrack,
which is untracked-tolerant, so this must NOT abort even on a debug build
where _PyObject_ASSERT_FROM is live.

This is the guarded twin of CPY-0015 (dictiter_dealloc, which uses the strict
_PyObject_GC_UNTRACK macro on the same shape and SIGABRTs).

Expected: "OK" and exit 0 on every build.
"""

import sys

N = 2000

for i in range(N):
    # set_update_local fails on the unhashable dict -> Py_DECREF(so) before track
    try:
        set([1, 2, 3, {}])
    except TypeError:
        pass

    # frozenset takes the same make_new_set path
    try:
        frozenset([1, 2, 3, []])
    except TypeError:
        pass

    # failure from a generator, i.e. arbitrary Python raising mid-fill
    def boom():
        yield 1
        yield 2
        raise ValueError("boom")

    try:
        set(boom())
    except ValueError:
        pass

    # set subclass -> make_new_set_basetype path
    class S(set):
        pass

    try:
        S([1, {}])
    except TypeError:
        pass

print("OK", N, "iterations", file=sys.stderr)
sys.exit(0)

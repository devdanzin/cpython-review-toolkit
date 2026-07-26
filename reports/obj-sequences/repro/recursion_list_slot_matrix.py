"""recursion-guard-auditor, slice obj-sequences.

Hand-enumeration probe for EVERY recursion-capable slot / comparison path
reachable from `Objects/listobject.c`.  The question each scenario answers is
the only one that matters for this bug class:

    does a deeply-nested or self-referential list drive the descent into a
    catchable RecursionError (guard present) or into an uncatchable native
    stack overflow (guard absent)?

The guard being tested is `_Py_EnterRecursiveCallTstate`, which since 3.14 is a
real C-stack-pointer check (`Include/internal/pycore_ceval.h:211-220`,
`here_addr < tstate->c_stack_soft_limit`) and therefore bounds pure-C recursion
independently of `sys.setrecursionlimit`.  So:

    RecursionError  -> the descent is bounded            (rc = 0)
    SIGSEGV / 139   -> native C stack overflow           (the bug class)

Each scenario runs in its own subprocess so a hard crash is attributable.

Usage:
    <python> recursion_list_slot_matrix.py            # run every scenario
    <python> recursion_list_slot_matrix.py <name>     # run one, in-process
"""

import os
import subprocess
import sys

DEPTH = int(os.environ.get("RECUR_DEPTH", "200000"))


def _deep(n=DEPTH):
    """Return an n-deep nest of one-element lists: [[[...]]]."""
    x = []
    for _ in range(n):
        x = [x]
    return x


def _drop(x):
    """Unwind a deep nest iteratively so teardown never confounds a scenario."""
    while isinstance(x, list) and x:
        x = x.pop()


# --------------------------------------------------------------------------
# tp_repr / tp_str  -- Py_ReprEnter + PyObject_Repr (Objects/object.c:780)
# --------------------------------------------------------------------------


def s_repr_deep():
    a = _deep()
    try:
        repr(a)
    finally:
        _drop(a)


def s_repr_cycle():
    a = []
    a.append(a)
    print("VAL", repr(a))
    a.clear()


def s_str_deep():
    a = _deep()
    try:
        str(a)
    finally:
        _drop(a)


def s_format_deep():
    # %R inside PyUnicode_FromFormat, and f-string !r -- both route through
    # PyObject_Repr, so both should be bounded too.
    a = _deep()
    try:
        "{!r}".format(a)
    finally:
        _drop(a)


# --------------------------------------------------------------------------
# tp_richcompare -- list_richcompare_impl, the single scanner finding
# --------------------------------------------------------------------------


def s_eq_deep():
    a, b = _deep(), _deep()
    try:
        print("VAL", a == b)
    finally:
        _drop(a)
        _drop(b)


def s_ne_deep():
    a, b = _deep(), _deep()
    try:
        print("VAL", a != b)
    finally:
        _drop(a)
        _drop(b)


def s_lt_deep():
    a, b = _deep(), _deep()
    try:
        print("VAL", a < b)
    finally:
        _drop(a)
        _drop(b)


def s_eq_cycle():
    # The textbook self-referential comparison: two distinct cyclic lists.
    a = []
    a.append(a)
    b = []
    b.append(b)
    try:
        print("VAL", a == b)
    finally:
        a.clear()
        b.clear()


def s_lt_cycle():
    a = []
    a.append(a)
    b = []
    b.append(b)
    try:
        print("VAL", a < b)
    finally:
        a.clear()
        b.clear()


def s_eq_self_cycle():
    # identity fast path in PyObject_RichCompareBool -- must NOT recurse
    a = []
    a.append(a)
    try:
        print("VAL", a == a)
    finally:
        a.clear()


# --------------------------------------------------------------------------
# DISPATCHER-BYPASSING ROUTES into list_richcompare.
#
# The scanner classifies list_richcompare_impl as
# `recursion_descent_guarded_by_dispatcher` and says: promote only if the slot
# is reached by a route that bypasses PyObject_RichCompare.  Two such routes
# exist and are exercised here:
#
#   R1  Objects/listobject.c:2787 -- `(*(ms->key_richcompare))(v, w, Py_LT)`
#       in unsafe_object_compare.  list.sort() on a homogeneous list of exact
#       lists sets ms->key_richcompare = list_richcompare (:3079) and calls it
#       through the cached slot pointer.  Confirmed under gdb: no
#       PyObject_RichCompare frame on the stack.
#   R2  Objects/typeobject.c:10253 -- wrap_richcmpfunc, the slot wrapper behind
#       `list.__eq__(a, b)` from Python: `return (*func)(self, other, op);`
#
# Both add exactly ONE unguarded frame; the recursive step inside
# list_richcompare_impl re-enters through PyObject_RichCompareBool, which is
# guarded.  These scenarios test whether that reasoning holds at depth.
# --------------------------------------------------------------------------


def s_eq_dunder_deep():
    a, b = _deep(), _deep()
    try:
        print("VAL", list.__eq__(a, b))
    finally:
        _drop(a)
        _drop(b)


def s_lt_dunder_deep():
    a, b = _deep(), _deep()
    try:
        print("VAL", list.__lt__(a, b))
    finally:
        _drop(a)
        _drop(b)


def s_eq_dunder_cycle():
    a = []
    a.append(a)
    b = []
    b.append(b)
    try:
        print("VAL", list.__eq__(a, b))
    finally:
        a.clear()
        b.clear()


# --------------------------------------------------------------------------
# sq_contains / index / count / remove -- PyObject_RichCompareBool per element
# --------------------------------------------------------------------------


def s_contains_deep():
    a, b = _deep(), _deep()
    try:
        print("VAL", a in [b])
    finally:
        _drop(a)
        _drop(b)


def s_index_deep():
    a, b = _deep(), _deep()
    try:
        [b].index(a)
    finally:
        _drop(a)
        _drop(b)


def s_count_deep():
    a, b = _deep(), _deep()
    try:
        print("VAL", [b].count(a))
    finally:
        _drop(a)
        _drop(b)


def s_remove_deep():
    a, b = _deep(), _deep()
    try:
        [b].remove(a)
    finally:
        _drop(a)
        _drop(b)


def s_contains_cycle():
    a = []
    a.append(a)
    b = []
    b.append(b)
    try:
        print("VAL", a in [b])
    finally:
        a.clear()
        b.clear()


# --------------------------------------------------------------------------
# list.sort -- safe_object_compare / unsafe_object_compare / unsafe_tuple_compare
# --------------------------------------------------------------------------


def s_sort_deep():
    a, b = _deep(), _deep()
    try:
        [a, b].sort()
    finally:
        _drop(a)
        _drop(b)


def s_sort_tuple_deep():
    # unsafe_tuple_compare path: exact tuples of deeply-nested lists
    a, b = _deep(), _deep()
    try:
        [(a,), (b,)].sort()
    finally:
        _drop(a)
        _drop(b)


def s_sort_key_deep():
    a, b = _deep(), _deep()
    try:
        [1, 2].sort(key=lambda _i, _k=[a, b]: _k[_i - 1])
    finally:
        _drop(a)
        _drop(b)


def s_sort_reentrant_same():
    """A user __lt__ that sorts the SAME list that is already being sorted.

    listobject.c:2968-2973 detaches ob_item/ob_size/allocated before any user
    code runs, so the re-entrant sort sees an empty list.  This checks whether
    the detach also bounds recursion (it does not have to -- each level is a
    fresh C frame).
    """
    lst = []

    class Item:
        def __init__(self, n):
            self.n = n

        def __lt__(self, other):
            lst.sort()  # re-enter the sort of the very list being sorted
            return self.n < other.n

    lst.extend(Item(i) for i in (2, 1))
    lst.sort()
    print("VAL reentrant-same ok, len=", len(lst))


def s_sort_reentrant_nested():
    """A user __lt__ that starts a NEW sort each level -- unbounded nesting.

    Each level costs a Python frame *and* a list_sort_impl C frame.  If the
    Python frame guard fires first this is a clean RecursionError.
    """

    class Item:
        __slots__ = ("n",)

        def __init__(self, n):
            self.n = n

        def __lt__(self, other):
            [Item(self.n + 1), Item(self.n)].sort()
            return True

    [Item(0), Item(0)].sort()


def s_sort_cmp_cycle():
    """__lt__ that triggers a self-referential list comparison mid-sort."""
    a = []
    a.append(a)
    b = []
    b.append(b)

    class Item:
        def __lt__(self, other):
            return a == b  # cyclic list_richcompare from inside the sort

    try:
        [Item(), Item()].sort()
    finally:
        a.clear()
        b.clear()


# --------------------------------------------------------------------------
# tp_hash / tp_traverse / tp_dealloc
# --------------------------------------------------------------------------


def s_hash_list():
    try:
        hash([1, 2, 3])
    except TypeError as exc:
        print("VAL TypeError:", exc)


def s_dealloc_deep():
    # Drop a deep nest with NO iterative unwind: exercises list_dealloc's
    # Py_DECREF chain (bounded by the automatic trashcan in _Py_Dealloc).
    a = _deep()
    a = None
    print("VAL dealloc ok")


def s_traverse_deep():
    import gc

    a = _deep(min(DEPTH, 50000))
    inner = a
    while inner and isinstance(inner[0], list):
        inner = inner[0]
    inner.append(a)  # make the whole chain one cycle so gc must collect it
    a = None
    inner = None
    gc.collect()
    print("VAL traverse/gc ok")


SCENARIOS = {k[2:]: v for k, v in sorted(globals().items()) if k.startswith("s_")}


def main(argv):
    if len(argv) > 1:
        name = argv[1]
        fn = SCENARIOS[name]
        try:
            fn()
        except RecursionError as exc:
            print(f"PROBE:{name}=RecursionError ({exc})", flush=True)
            return 0
        except BaseException as exc:  # noqa: BLE001 - probe
            print(f"PROBE:{name}={type(exc).__name__}: {exc}", flush=True)
            return 0
        print(f"PROBE:{name}=completed", flush=True)
        return 0

    for name in SCENARIOS:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), name],
            capture_output=True,
            text=True,
            timeout=600,
        )
        out = " | ".join(
            line for line in proc.stdout.splitlines() if line.startswith(("PROBE", "VAL"))
        )
        tail = proc.stderr.strip().splitlines()[-1:] if proc.stderr.strip() else []
        print(f"{name:24s} rc={proc.returncode:<5d} {out}  {' '.join(tail)[:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

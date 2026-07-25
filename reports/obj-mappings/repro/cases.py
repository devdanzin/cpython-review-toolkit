"""Recursion-class probe cases for the obj-mappings slice.

Each function is one hypothesis about a descent in Objects/dictobject.c or
Objects/setobject.c.  Run via probe_driver.py, which executes each in its own
subprocess and records the exit code (-11 == SIGSEGV, 0 == survived,
1 == clean Python exception).

Ground rule (agent brief lesson 4): a crash counts only if it was RUN.
"""

import sys

N = 200_000


# --------------------------------------------------------------------------
# CPY-0019 (already recorded) -- frozendict_pair_hash:8427 descends VALUES.
# Values are never hashed at insertion, so nothing is primed: one hash() call
# walks all N levels.
# --------------------------------------------------------------------------
def frozendict_hash_deep_values():
    d = __builtins__.frozendict({})
    for _ in range(N):
        d = __builtins__.frozendict({0: d})
    print("built", flush=True)
    hash(d)


# --------------------------------------------------------------------------
# The frozenset twin.  frozenset_hash_impl:989 XORs entry->hash -- the hash
# cached at INSERTION -- so it never calls PyObject_Hash.  Depth should be
# irrelevant.
# --------------------------------------------------------------------------
def frozenset_hash_deep():
    x = frozenset()
    for _ in range(N):
        x = frozenset([x])
    print("built", flush=True)
    hash(x)


def frozenset_hash_deep_no_prime():
    """Same shape, but force the whole chain to exist before any hash() call.

    frozenset construction *must* hash each element (set_add_key:603), so the
    chain cannot be built without priming -- this case exists to prove that,
    not to dodge it.
    """
    x = frozenset()
    for _ in range(N):
        x = frozenset([x])
    y = frozenset([x])
    print("built", flush=True)
    hash(y)


def frozenset_selfcontaining():
    """Is a self-containing frozenset constructible at all from Python?"""
    s = set()
    try:
        s.add(s)
        print("set contains itself?!", flush=True)
    except TypeError as e:
        print("set.add(self) ->", type(e).__name__, e, flush=True)
    try:
        frozenset([s])
        print("frozenset([set]) OK?!", flush=True)
    except TypeError as e:
        print("frozenset([set]) ->", type(e).__name__, e, flush=True)
    # via the membership-testing escape hatch in _PySet_Contains:2570
    print("set in frozenset ->", s in frozenset(), flush=True)


# --------------------------------------------------------------------------
# repr: Py_ReprEnter/Py_ReprLeave guard CYCLES.  DEPTH is bounded by the
# PyObject_Repr dispatcher (Objects/object.c:759), not by Py_ReprEnter.
# --------------------------------------------------------------------------
def dict_repr_deep():
    d = {}
    for _ in range(N):
        d = {0: d}
    print("built", flush=True)
    repr(d)


def dict_repr_cycle():
    d = {}
    d[0] = d
    print(repr(d), flush=True)


def frozendict_repr_deep():
    d = __builtins__.frozendict({})
    for _ in range(N):
        d = __builtins__.frozendict({0: d})
    print("built", flush=True)
    repr(d)


def frozendict_repr_cycle():
    d = {}
    fd = __builtins__.frozendict({0: d})
    d[0] = fd
    print(repr(fd), flush=True)


def set_repr_deep():
    x = frozenset()
    for _ in range(N):
        x = frozenset([x])
    print("built", flush=True)
    repr(x)


def set_repr_cycle():
    s = set()
    lst = [s]
    s.add((0,))
    # a set cannot contain itself; go through a list to make the cycle
    lst.append(lst)
    print(repr(s), repr(lst)[:40], flush=True)


def dictview_repr_deep():
    """NOTE: d.keys() of {0: <deep>} is just [0] -- no descent.  Kept as the
    control; dictview_items_repr_deep / dictview_values_repr_deep descend."""
    d = {}
    for _ in range(N):
        d = {0: d}
    print("built", flush=True)
    repr(d.keys())


def dictview_items_repr_deep():
    d = {}
    for _ in range(N):
        d = {0: d}
    print("built", flush=True)
    repr(d.items())


def dictview_values_repr_deep():
    d = {}
    for _ in range(N):
        d = {0: d}
    print("built", flush=True)
    repr(d.values())


def dictview_repr_cycle():
    d = {}
    d[0] = d.items()
    print(repr(d.items()), flush=True)


# --------------------------------------------------------------------------
# richcompare: dict_equal_lock_held:4713 and set_compare_*:112/155/185 all
# descend through PyObject_RichCompareBool, which IS dispatcher-guarded
# (Objects/object.c:1099/1121).
# --------------------------------------------------------------------------
def _deep_dict(n):
    d = {}
    for _ in range(n):
        d = {0: d}
    return d


def dict_eq_deep():
    a, b = _deep_dict(N), _deep_dict(N)
    print("built", flush=True)
    print(a == b, flush=True)


def frozendict_eq_deep():
    fd = __builtins__.frozendict

    def build():
        d = fd({})
        for _ in range(N):
            d = fd({0: d})
        return d

    a, b = build(), build()
    print("built", flush=True)
    print(a == b, flush=True)


def frozenset_eq_deep():
    def build():
        x = frozenset()
        for _ in range(N):
            x = frozenset([x])
        return x

    a, b = build(), build()
    print("built", flush=True)
    print(a == b, flush=True)


def frozenset_issubset_deep():
    def build():
        x = frozenset()
        for _ in range(N):
            x = frozenset([x])
        return x

    a, b = frozenset([build()]), frozenset([build()])
    print("built", flush=True)
    print(a.issubset(b), a - b, a | b, a & b, a ^ b, a.isdisjoint(b), flush=True)


def dictview_eq_deep():
    a, b = _deep_dict(N), _deep_dict(N)
    print("built", flush=True)
    print(a.items() == b.items(), flush=True)


# --------------------------------------------------------------------------
# Entry points (scan_recursion_guards shape `hash_entry_point`): these add ONE
# C frame; the recursive frames belong to the argument's tp_hash.  Included as
# blast-radius evidence, not as findings.
# --------------------------------------------------------------------------
def _deep_frozendict(n):
    fd = __builtins__.frozendict
    d = fd({})
    for _ in range(n):
        d = fd({0: d})
    return d


def entry_set_add_key():
    """setobject.c:603 set_add_key -> PyObject_Hash -> frozendict_hash chain."""
    d = _deep_frozendict(N)
    print("built", flush=True)
    {d}


def entry_dict_setitem():
    """dictobject.c:2823 setitem_take2_lock_held -> _PyObject_HashDictKey."""
    d = _deep_frozendict(N)
    print("built", flush=True)
    {d: 1}


def entry_dict_contains():
    """dictobject.c:5278 dict_contains -> _PyObject_HashDictKey."""
    d = _deep_frozendict(N)
    print("built", flush=True)
    print(d in {}, flush=True)


def entry_set_contains():
    """setobject.c:614 set_contains_key -> PyObject_Hash."""
    d = _deep_frozendict(N)
    print("built", flush=True)
    print(d in set(), flush=True)


def entry_dictitems_or():
    """d.items() | set() hashes every (k, v) tuple -- so it hashes VALUES that
    were never hashed on insertion.  dictviews_to_set:6662 -> PySet_New."""
    deep = _deep_frozendict(N)
    d = {0: deep}
    print("built", flush=True)
    d.items() | set()


def entry_dict_fromkeys():
    d = _deep_frozendict(N)
    print("built", flush=True)
    dict.fromkeys([d])


# --------------------------------------------------------------------------
# Deep TUPLE reached through the slice's entry points -- this is CPY-0001
# (tuple_hash) arriving via dict/set, not a new site.
# --------------------------------------------------------------------------
def entry_set_add_deep_tuple():
    t = ()
    for _ in range(N):
        t = (t,)
    print("built", flush=True)
    {t}


def entry_frozendict_value_deep_tuple():
    t = ()
    for _ in range(N):
        t = (t,)
    fd = __builtins__.frozendict({0: t})
    print("built", flush=True)
    hash(fd)


CASES = {k: v for k, v in sorted(globals().items())
         if callable(v) and not k.startswith("_") and v.__module__ == __name__}

if __name__ == "__main__":
    name = sys.argv[1]
    if len(sys.argv) > 2:
        N = int(sys.argv[2])
        globals()["N"] = N
    sys.setrecursionlimit(10_000_000)
    CASES[name]()
    print("SURVIVED", flush=True)

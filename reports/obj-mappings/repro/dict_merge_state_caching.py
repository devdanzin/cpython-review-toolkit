"""CPY-0142 probe: dict_merge's slow path (dictobject.c:4321-4346) holds
Py_BEGIN_CRITICAL_SECTION(a) across PyMapping_Keys / PyObject_GetIter /
PyIter_Next / dict_contains / PyObject_GetItem -- five calls that all run
arbitrary Python -- with `mp = _PyAnyDict_CAST(a)` cached at :4306 and used at
:4373 setitem_lock_held(mp, key, value).

Question the agents raised and never answered: is any state cached across those
Python-reaching calls stale-able?

This probe re-enters `a` from every one of the five call sites and checks:
  (1) does anything crash / assert?
  (2) is the result merely non-atomic, or actually corrupt?
"""

import sys

FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  {name}: {'ok' if ok else 'MISMATCH'} got={got!r} want={want!r}")
    if not ok:
        FAIL.append(name)


# ---- 1. re-enter from keys() -------------------------------------------
def probe_keys():
    a = {"seed": 0}

    class M:
        def keys(self):
            a.clear()
            for i in range(50):
                a[f"x{i}"] = i
            a.clear()
            return ["k1", "k2"]

        def __getitem__(self, k):
            return k.upper()

    a.update(M())
    print("  probe_keys ->", dict(sorted(a.items())))


# ---- 2. re-enter from the keys iterator --------------------------------
def probe_iter():
    a = {"seed": 0}

    class Keys:
        def __iter__(self):
            for i in range(40):
                yield f"k{i}"
                a.clear()
                for j in range(60):
                    a[f"filler{j}"] = j

    class M:
        def keys(self):
            return Keys()

        def __getitem__(self, k):
            return 1

    a.update(M())
    print("  probe_iter -> len", len(a))


# ---- 3. re-enter from dict_contains (override != 1) --------------------
def probe_contains():
    # dict.update() uses override==1, so dict_contains is skipped.  The
    # override!=1 paths are PyDict_Merge(override=0) and dict_merge_api via
    # `|=`-style helpers.  From pure Python the reachable one is
    # collections-style: dict.setdefault-free path -> use the C API through
    # `{}.update` is override=1.  Use a str subclass with a mutating __eq__
    # so lookup of the key inside setitem_lock_held runs Python.
    class EvilKey(str):
        target = None

        def __hash__(self):
            return hash("collide")

        def __eq__(self, other):
            if EvilKey.target is not None:
                EvilKey.target.clear()
                for i in range(80):
                    EvilKey.target[f"z{i}"] = i
            return str.__eq__(self, other)

    a = {}
    EvilKey.target = a

    class M:
        def keys(self):
            return [EvilKey("collide"), "collide", EvilKey("collide")]

        def __getitem__(self, k):
            return 7

    a.update(M())
    EvilKey.target = None
    print("  probe_contains -> len", len(a))


# ---- 4. re-enter from __getitem__ (the value fetch) --------------------
def probe_getitem():
    a = {"seed": 0}

    class M:
        def keys(self):
            return [f"k{i}" for i in range(40)]

        def __getitem__(self, k):
            a.clear()
            for i in range(70):
                a[f"y{i}"] = i
            a.clear()
            return k

    a.update(M())
    print("  probe_getitem -> len", len(a), "sample", list(a)[:3])


# ---- 5. does the re-entrant view of `a` observe partial state? ----------
def probe_visibility():
    a = {}
    seen = []

    class M:
        def keys(self):
            return ["a", "b", "c", "d"]

        def __getitem__(self, k):
            seen.append(dict(a))
            return k

    a.update(M())
    print("  probe_visibility -> re-entrant snapshots:", seen)
    # Under a correct lock the re-entrant read would be blocked; it is not,
    # because same-thread critical-section re-acquisition is elided.
    check(
        "partial state visible to re-entrant reader",
        any(s for s in seen),
        True,
    )


for fn in (probe_keys, probe_iter, probe_contains, probe_getitem, probe_visibility):
    print(fn.__name__)
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        print("  raised", type(exc).__name__, exc)

print("FAILURES:", FAIL)
print("DONE")
sys.exit(0)

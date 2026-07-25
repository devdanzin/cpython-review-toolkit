"""set.add() reports success but silently drops the element.

set_add_entry_takeref (Objects/setobject.c:288-295) runs an arbitrary user
__eq__ via PyObject_RichCompareBool, then:

    if (cmp > 0)  goto found_active;              <- 290, NO revalidation
    if (cmp < 0)  goto comparison_error;          <- 292
    if (table != so->table || entry->key != startkey) goto restart;   <- 294

The mutation-revalidation guard is only reached when cmp == 0.  Its sibling
reader, set_compare_entry_lock_held (:157-162), orders the same three checks
so that revalidation happens BEFORE the FOUND verdict:

    if (cmp < 0) return SET_LOOKKEY_ERROR;
    if (table != so->table || entry->key != startkey) return SET_LOOKKEY_CHANGED;
    if (cmp > 0) return SET_LOOKKEY_FOUND;

So if the user's __eq__ removes the matching element and then claims equality,
the writer takes the "already present" path against a slot that no longer holds
that element.  found_active does Py_DECREF(key) and returns 0 -- success -- and
the element is gone.

found_active does NOT dereference `entry`, so this is NOT a use-after-free.
It is a silent lost update: add() succeeds, the element is not in the set.
"""

import sys


class Vanish:
    """Removes itself from the set during comparison, then claims equality."""

    fired = False

    def __init__(self, target):
        self.target = target

    def __hash__(self):
        return 12345  # force a hash match so RichCompareBool is reached

    def __eq__(self, other):
        if not Vanish.fired:
            Vanish.fired = True
            self.target.discard(self)
        return True


def main():
    results = []

    # --- case 1: the element removes itself mid-compare, then says "equal" ---
    Vanish.fired = False
    s = set()
    a = Vanish(s)
    s.add(a)
    a.target = s

    b = Vanish(s)
    before = (len(s), a in s)
    rc = s.add(b)  # returns None; the point is that it does not raise
    after = (len(s), b in s, a in s)

    results.append(("self-discarding __eq__", before, after, rc))
    lost = (len(s) == 0)

    # --- case 2: control -- same shape but __eq__ does not mutate ---
    class Inert:
        def __hash__(self):
            return 12345

        def __eq__(self, other):
            return True

    s2 = set()
    c = Inert()
    s2.add(c)
    d = Inert()
    s2.add(d)
    control_ok = (len(s2) == 1 and c in s2)

    # --- case 3: the reader path on the same hostile object, for contrast ---
    Vanish.fired = False
    s3 = set()
    e = Vanish(s3)
    s3.add(e)
    e.target = s3
    f = Vanish(s3)
    try:
        reader = (f in s3)  # set_contains -> set_compare_entry_lock_held
        reader_err = None
    except BaseException as exc:  # noqa: BLE001
        reader = None
        reader_err = repr(exc)

    print("=== set.add() lost-element probe ===")
    for name, bef, aft, rc in results:
        print(f"{name}:")
        print(f"  before add: len={bef[0]} original_present={bef[1]}")
        print(f"  after  add: len={aft[0]} new_present={aft[1]} original_present={aft[2]}")
        print(f"  add() returned {rc!r} (did not raise)")
    print(f"control (non-mutating __eq__): len==1 and present -> {control_ok}")
    print(f"reader path (x in s) with same hostile __eq__: {reader!r} err={reader_err!r}")
    print()
    print(f"VERDICT lost_element={lost}")
    return 0 if not lost else 7


if __name__ == "__main__":
    sys.exit(main())

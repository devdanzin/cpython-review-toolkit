"""CPY-0116 -- reversed(dict) out-of-bounds read: all entry points + the split-table probe.

Run one variant per subprocess:

    python issue_CPY-0116_variants.py {dict|dunder|keys|values|items}
    python issue_CPY-0116_variants.py split:{shrink-restore|clear-restore|combined|clear-one}

The five combined-table entry points all SIGSEGV. The split-table probes do NOT --
see the note at the bottom of this file, which is the point of including them.
"""

import sys

N = 1000


def stale_combined_iter(make):
    """A reverse iterator whose di_pos is stale by ~N against a 5-slot table.

    di_pos is seeded from dk_nentries - 1 for a COMBINED table
    (dictobject.c:5636). The only staleness check is di_used != ma_used
    (dictobject.c:6261), which compares ma_used and says nothing about
    dk_nentries -- so replacing the keys object with a smaller one while
    keeping the element count equal walks straight past it.
    """
    d = {}
    for i in range(N):
        d["k%d" % i] = i          # combined table, dk_nentries == N
    for i in range(1, N):
        del d["k%d" % i]          # ma_used == 1, dk_nentries still N

    it = make(d)                  # di_pos = dk_nentries - 1 = N-1

    d.clear()                     # fresh PyDict_MINSIZE keys object
    d["k0"] = 0                   # ma_used == 1 again -> staleness check passes
    return it


ENTRY_POINTS = {
    "dict":   lambda d: reversed(d),
    "dunder": lambda d: d.__reversed__(),
    "keys":   lambda d: reversed(d.keys()),
    "values": lambda d: reversed(d.values()),
    "items":  lambda d: reversed(d.items()),
}


class C:
    pass


def make_split(n):
    """An instance __dict__ backed by a shared (split) keys table."""
    for _ in range(3):                    # warm the shared keys
        w = C()
        for i in range(n):
            setattr(w, "a%d" % i, i)
    o = C()
    for i in range(n):
        setattr(o, "a%d" % i, i)
    return o


def split_probe(mode, n=20):
    """Try to drive the SPLIT branch (dictobject.c:6274-6279) out of bounds.

    That branch calls get_index_from_order(d, i), whose only bound is
    `assert(i < mp->ma_values->size)` -- which compiles out under NDEBUG.
    """
    o = make_split(n)
    d = o.__dict__
    it = reversed(d)                      # di_pos = ma_used - 1

    if mode == "shrink-restore":
        for i in range(1, n):
            delattr(o, "a%d" % i)         # ma_used -> 1
        for i in range(1, n):
            setattr(o, "b%d" % i, i)      # ma_used -> n again, different keys
    elif mode == "clear-restore":
        d.clear()
        for i in range(n):
            setattr(o, "c%d" % i, i)
    elif mode == "combined":
        d[object()] = 1                   # non-str key forces combined
    elif mode == "clear-one":
        d.clear()
        o.a0 = 0
    else:
        raise SystemExit("unknown split mode %r" % mode)
    return it


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "dict"
    if which.startswith("split:"):
        it = split_probe(which.split(":", 1)[1])
    else:
        it = stale_combined_iter(ENTRY_POINTS[which])
    try:
        for _ in it:
            pass
    except RuntimeError as exc:
        print("RuntimeError: %s" % exc)
        return 0
    print("survived %s" % which)
    return 0


if __name__ == "__main__":
    sys.exit(main())


# Measured on release-gil-nojit and debug-gil-nojit (CPython main, 3.16.0a0):
#
#   dict dunder keys values items    -> SIGSEGV, 10/10 (5 entry points x 2 builds)
#   split:shrink-restore             -> survived, 2/2
#   split:clear-restore              -> survived, 2/2
#   split:combined                   -> RuntimeError (staleness check fires), 2/2
#   split:clear-one                  -> RuntimeError (staleness check fires), 2/2
#
# The split branch is not reachable by this route, and the reason is worth
# stating: its seed is `ma_used - 1`, the very quantity the staleness check
# pins, and any table holding ma_used entries has at least that many slots.
# The combined seed is `dk_nentries - 1`, which the staleness check does not
# constrain at all. That asymmetry -- not the missing bound alone -- is the bug.

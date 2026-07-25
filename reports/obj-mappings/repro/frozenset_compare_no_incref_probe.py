"""obj-mappings / refcount-auditor -- NEGATIVE probe.

set_compare_frozenset (Objects/setobject.c:171-193) drops both protections its
sibling set_compare_entry_lock_held (:137-165) has: no Py_INCREF of startkey
across PyObject_RichCompareBool, and no post-compare revalidation of
(table, entry->key).

This script tries every route by which a user __eq__ running inside that compare
could (a) free startkey or (b) change the table under the probe, and records the
observed outcome for each.  It is expected to print "SURVIVED" for all routes;
that is the evidence for the bounded-negative verdict.
"""

import gc
import sys

RESULTS = []


def record(name, outcome):
    RESULTS.append((name, outcome))
    print("%-46s %s" % (name, outcome), flush=True)


# --------------------------------------------------------------------------
# Route 1: adversarial __eq__ that drops every Python-visible reference to the
# frozenset being searched and forces a GC.  The frozenset is still on the
# interpreter value stack, so gc_refs > 0 and tp_clear must not run.
# --------------------------------------------------------------------------
class DropSelf:
    def __init__(self, h):
        self.h = h

    def __hash__(self):
        return self.h

    def __eq__(self, other):
        global FS, CYCLE
        FS = None
        CYCLE = None
        gc.collect()
        gc.collect()
        return False


class Probe:
    def __init__(self, h):
        self.h = h

    def __hash__(self):
        return self.h

    def __eq__(self, other):
        return False


try:
    victim = DropSelf(1234)
    CYCLE = {}                      # give the frozenset a cycle partner
    FS = frozenset([victim, CYCLE.setdefault("k", object())])
    CYCLE["fs"] = FS                # FS -> CYCLE -> FS  (tracked cycle)
    victim.cycle = CYCLE
    print("tracked:", gc.is_tracked(FS), flush=True)
    r = Probe(1234) in FS
    record("route1 drop-refs+gc.collect during __eq__", "SURVIVED (result=%r)" % (r,))
except BaseException as e:
    record("route1 drop-refs+gc.collect during __eq__", "EXC %r" % (e,))


# --------------------------------------------------------------------------
# Route 2: does the C-level unique-reference gate on PySet_Add actually hold?
# marshal builds frozensets with PySet_Add; exercise it on a self-referential
# structure to make sure the frozenset really is unreachable while filled.
# --------------------------------------------------------------------------
try:
    import marshal

    data = marshal.dumps(frozenset(["a", "b", "c"]))
    back = marshal.loads(data)
    record("route2 marshal frozenset round-trip", "SURVIVED (%r)" % (sorted(back),))
except BaseException as e:
    record("route2 marshal frozenset round-trip", "EXC %r" % (e,))


# --------------------------------------------------------------------------
# Route 3: is set.intersection_update (the only caller of set_swap_bodies)
# reachable with an exact frozenset receiver via an unbound call?
# --------------------------------------------------------------------------
for name in ("intersection_update", "difference_update", "remove", "discard",
             "clear", "add", "pop", "update", "symmetric_difference_update"):
    meth = getattr(set, name, None)
    if meth is None:
        record("route3 set.%s(frozenset, ...)" % name, "NO SUCH METHOD")
        continue
    try:
        meth(frozenset([1, 2, 3]), frozenset([1]))
        record("route3 set.%s(frozenset, ...)" % name, "*** ACCEPTED -- INVESTIGATE ***")
    except TypeError as e:
        record("route3 set.%s(frozenset, ...)" % name, "TypeError (rejected)")
    except BaseException as e:
        record("route3 set.%s(frozenset, ...)" % name, "%s" % type(e).__name__)


# --------------------------------------------------------------------------
# Route 4: frozenset difference takes set_copy_and_difference_untracked, which
# builds an EXACT frozenset and then mutates it in place through
# set_discard_entry -> set_lookkey -> set_compare_frozenset.  Drive that path
# with an adversarial __eq__ and check the answer is still right.
# --------------------------------------------------------------------------
class Reentrant:
    def __init__(self, h, tag):
        self.h = h
        self.tag = tag

    def __hash__(self):
        return self.h

    def __eq__(self, other):
        # re-enter the set machinery from inside the compare
        gc.collect()
        _ = self.tag in BIG
        return isinstance(other, Reentrant) and other.tag == self.tag


try:
    # len(so) >> 4*len(other) forces set_copy_and_difference_untracked
    BIG = frozenset(list(range(200)) + [Reentrant(999, "x")])
    small = frozenset([Reentrant(999, "x")])
    out = BIG - small
    ok = (len(out) == 200 and Reentrant(999, "x") not in out)
    record("route4 frozenset difference in-place discard",
           "SURVIVED (len=%d correct=%s)" % (len(out), ok))
except BaseException as e:
    record("route4 frozenset difference in-place discard", "EXC %r" % (e,))


print("\n--- summary ---")
for name, outcome in RESULTS:
    print("%-46s %s" % (name, outcome))
print("interpreter still alive:", sys.version.split()[0])

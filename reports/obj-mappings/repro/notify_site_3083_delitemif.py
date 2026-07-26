"""notify site :3083 -- delitemif_lock_held, reached from Python via
_weakref._remove_dead_weakref(dct, key) (Modules/_weakref.c:59).

Objects/dictobject.c:3053-3089

    3070:  ix = _Py_dict_lookup(mp, key, hash, &old_value);   <-- both stale
    ...
    3083:  _PyDict_NotifyEvent(PyDict_EVENT_DELETED, mp, key, NULL);  <-- window
    3084:  delitem_common(mp, hash, ix, old_value);
             -> :2962 lookdict_index(mp->ma_keys, hash, ix); assert(hashpos>=0)
             -> :2976 dictkeys_set_index(mp->ma_keys, hashpos, DKIX_DUMMY)
             -> :2978 DK_*_ENTRIES(mp->ma_keys)[ix] WRITE
             -> :2992 Py_DECREF(old_value)   <-- stale borrowed ref

Structurally identical to the already-reproduced :3038, but on the
_PyDict_DelItemIf entry point, which promises the caller that the
predicate -> deletion sequence is atomic (the comment at :3090-3094).
The notify breaks exactly that promise.

Usage:  python notify_site_3083_delitemif.py [clear|regrow] [N]
"""

import sys
import weakref

import _testcapi
import _weakref

MODE = sys.argv[1] if len(sys.argv) > 1 else "clear"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 200


class C:
    pass


def main():
    d = {}
    for i in range(N):
        d["k%d" % i] = [i]

    # A dead weakref under the target key: the only reference to the weakref
    # object itself is the dict's, so clearing the dict frees it.
    target = "k%d" % (N - 1)
    o = C()
    d[target] = weakref.ref(o)
    del o  # the weakref is now dead -> is_dead_weakref() returns 1

    fired = []

    def hook(unraisable):
        if fired:
            return
        fired.append(1)
        d.clear()
        if MODE == "regrow":
            for j in range(3):
                d["r%d" % j] = [j]

    sys.unraisablehook = hook
    wid = _testcapi.add_dict_watcher(1)
    _testcapi.watch_dict(wid, d)
    print("[main] armed mode=%s N=%d" % (MODE, N), flush=True)

    _weakref._remove_dead_weakref(d, target)

    print("[main] _remove_dead_weakref returned", flush=True)
    _testcapi.unwatch_dict(wid, d)
    sys.unraisablehook = sys.__unraisablehook__
    print("[main] len=%d" % len(d), flush=True)
    print("[main] survived", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

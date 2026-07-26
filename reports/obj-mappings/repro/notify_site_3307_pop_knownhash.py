"""notify site :3307 -- _PyDict_Pop_KnownHash (reached by plain dict.pop()).

Objects/dictobject.c:

    3291:  Py_ssize_t ix = _Py_dict_lookup(mp, key, hash, &old_value);  <-- both stale
    ...
    3307:  _PyDict_NotifyEvent(PyDict_EVENT_DELETED, mp, key, NULL);   <-- window
    3308:  delitem_common(mp, hash, ix, Py_NewRef(old_value));
             -> :2962 lookdict_index(mp->ma_keys, hash, ix); assert(hashpos >= 0)
             -> :2976 dictkeys_set_index(mp->ma_keys, hashpos, DKIX_DUMMY)
             -> :2978 DK_*_ENTRIES(mp->ma_keys)[ix]  WRITE
             -> :2990 Py_DECREF(old_key)
    3312:  *result = old_value;   <-- handed back to Python

If the hook empties the dict, `old_value`'s only reference (the dict's) is
already gone, so :3308's Py_NewRef resurrects freed memory and :3312 returns a
dangling pointer to the Python caller.

Usage:  python notify_site_3307_pop_knownhash.py [clear|regrow] [N]
"""

import sys

import _testcapi

MODE = sys.argv[1] if len(sys.argv) > 1 else "clear"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 200
TARGET = "k%d" % (N - 1)


def main():
    d = {}
    for i in range(N):
        d["k%d" % i] = [i]  # heap values, refcount 1

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

    r = d.pop(TARGET)

    print("[main] pop returned", flush=True)
    _testcapi.unwatch_dict(wid, d)
    sys.unraisablehook = sys.__unraisablehook__
    print("[main] len=%d" % len(d), flush=True)
    print("[main] popped repr=%r" % (r,), flush=True)  # touches the returned object
    print("[main] survived", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

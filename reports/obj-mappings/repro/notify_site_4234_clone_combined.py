"""notify site :4234 -- dict_dict_merge, the CLONED fast path.

Objects/dictobject.c:4210-4245

    4219:  if (mp->ma_used == 0) {                  <-- guard, pre-notify
    4225:      PyDictKeysObject *okeys = other->ma_keys;
    4228:      if (mp->ma_values == NULL &&          <-- guard, pre-notify
                  other->ma_values == NULL &&       <-- guard, pre-notify
                  other->ma_used == okeys->dk_nentries && ...
    4234:          _PyDict_NotifyEvent(PyDict_EVENT_CLONED, mp, other, NULL);  <-- window
    4235:          PyDictKeysObject *keys = clone_combined_dict_keys(other);
                     -> asserts orig->ma_values == NULL
                     -> asserts orig->ma_keys != Py_EMPTY_KEYS
                     -> asserts orig->ma_keys->dk_refcnt == 1
    4240:          dictkeys_decref(mp->ma_keys, ...);
    4241:          set_keys(mp, keys);
    4242:          STORE_USED(mp, other->ma_used);

Every guard is evaluated before the notify and none is re-checked after it.

Modes:

  insert    the hook inserts into `mp` (which the pre-notify guard proved
            empty).  :4240-4242 then replaces mp's keys wholesale and
            overwrites ma_used, so everything the hook stored is silently
            discarded -- a wrong Python-visible result with no error.

  clearsrc  the hook clears `other`, so clone_combined_dict_keys() runs against
            Py_EMPTY_KEYS, violating its own assertion at :1028.

  growsrc   the hook grows `other` past the size the guard measured.

Usage:  python notify_site_4234_clone_combined.py [insert|clearsrc|growsrc]
"""

import sys

import _testcapi

MODE = sys.argv[1] if len(sys.argv) > 1 else "insert"


def main():
    # Small, clean, combined, freshly built: satisfies the :4228-4232 guard.
    src = {"a": 1, "b": 2, "c": 3}
    d = {}

    fired = []

    def hook(unraisable):
        if fired:
            return
        fired.append(1)
        if MODE == "insert":
            d["injected"] = ["injected-value"]
            d["injected2"] = ["injected-value2"]
        elif MODE == "clearsrc":
            src.clear()
        elif MODE == "growsrc":
            for j in range(40):
                src["g%d" % j] = j
        else:
            raise SystemExit("unknown mode %r" % MODE)

    sys.unraisablehook = hook
    wid = _testcapi.add_dict_watcher(1)
    _testcapi.watch_dict(wid, d)
    print("[main] armed mode=%s" % MODE, flush=True)

    d.update(src)

    print("[main] update returned", flush=True)
    _testcapi.unwatch_dict(wid, d)
    sys.unraisablehook = sys.__unraisablehook__
    print("[main] len(d)=%d" % len(d), flush=True)
    print("[main] d=%r" % (d,), flush=True)
    print("[main] src=%r" % (src,), flush=True)
    real = len(list(d.keys()))
    if len(d) != real:
        print("[main] *** ma_used=%d real entries=%d ***" % (len(d), real), flush=True)
    if MODE == "insert" and "injected" not in d:
        print("[main] *** LOST: the hook's stores vanished from d ***", flush=True)

    # Second-order check for `clearsrc`: in a release build clone_combined_dict_keys
    # happily memcpy's empty_keys_struct, whose dk_refcnt is
    # _Py_DICT_IMMORTAL_INITIAL_REFCNT (dictobject.c:646).  d now owns a HEAP keys
    # object carrying an immortal refcount, so clear_lock_held's
    # `assert(oldkeys->dk_refcnt == 1)` at :3148 no longer holds and the block can
    # never be freed.
    for j in range(50):
        d["post%d" % j] = j
    print("[main] post-insert len=%d ok=%s" % (len(d), d.get("post49") == 49), flush=True)
    d.clear()
    print("[main] post-clear len=%d" % len(d), flush=True)
    print("[main] survived", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

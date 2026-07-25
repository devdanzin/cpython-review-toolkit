"""The `_PyDict_NotifyEvent` stale-state family in Objects/dictobject.c.

Every one of these computes an entry index (or caches a raw entry pointer, or a
keys pointer) BEFORE `_PyDict_NotifyEvent`, which can run arbitrary Python via
`_PyDict_SendEvent` -> `PyErr_FormatUnraisable` (dictobject.c:8314) ->
`sys.unraisablehook`, and then uses it afterwards with no re-validation:

  scenario   site                                        stale value used after
  --------   ------------------------------------------  ----------------------
  delitem    :3038 _PyDict_DelItem_KnownHash_LockHeld ->  `ix` -> lookdict_index
             :2969-2987 delitem_common                    + DK_*_ENTRIES[ix] WRITE
  pop        :3307 _PyDict_Pop_KnownHash_LockHeld    ->   same
  modify     :2060 insertdict                             `ix` -> DK_*_ENTRIES[ix] WRITE
  empty      :2103 insert_to_emptydict                    `newkeys` published at :2124
                                                          OVER a keys object a
                                                          re-entrant insert already
                                                          published -> leak + ma_used
                                                          desync

Run one scenario per process:

    python notify_event_stale_ix_family.py {delitem|pop|modify|empty}
"""

import sys

import _testcapi


SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "delitem"


def build(n=200):
    d = {}
    for i in range(n):
        d["k%d" % i] = i
    return d


def main():
    fired = []

    if SCENARIO == "empty":
        d = {}

        def hook(unraisable):
            if fired:
                return
            fired.append(1)
            # Re-entrant insert while the outer insert_to_emptydict still holds
            # its own unpublished `newkeys`.
            d["inner"] = 1

        sys.unraisablehook = hook
        wid = _testcapi.add_dict_watcher(1)
        _testcapi.watch_dict(wid, d)
        print("[main] armed (empty dict)", flush=True)
        d["outer"] = 2
        _testcapi.unwatch_dict(wid, d)
        sys.unraisablehook = sys.__unraisablehook__
        print("[main] len(d)=%d  keys=%r" % (len(d), sorted(d)), flush=True)
        print("[main] list(d.items())=%r" % (sorted(d.items()),), flush=True)
        if len(d) != len(list(d.keys())):
            print("[main] *** ma_used (%d) != real entries (%d) ***"
                  % (len(d), len(list(d.keys()))), flush=True)
        print("[main] survived", flush=True)
        return 0

    d = build()

    def hook(unraisable):
        if fired:
            return
        fired.append(1)
        if SCENARIO == "doubleclear":
            # clear_lock_held caches oldkeys/oldvalues at :3136-3137 BEFORE its
            # own _PyDict_NotifyEvent(CLEARED) at :3142; a re-entrant resize
            # here replaces and frees them, and the outer frame still
            # dictkeys_decref(oldkeys) at :3149.
            for j in range(4000):
                d["grow%d" % j] = j
            return
        # Frees the keys object outright: clear_lock_held -> dictkeys_decref
        # -> free_keys_object, and installs Py_EMPTY_KEYS (a ZERO-entry table).
        d.clear()

    sys.unraisablehook = hook
    wid = _testcapi.add_dict_watcher(1)
    _testcapi.watch_dict(wid, d)
    print("[main] armed, scenario=%s, len=%d" % (SCENARIO, len(d)), flush=True)

    if SCENARIO == "delitem":
        del d["k199"]
    elif SCENARIO == "pop":
        d.pop("k199")
    elif SCENARIO == "modify":
        d["k199"] = "replacement"
    elif SCENARIO == "doubleclear":
        d.clear()
    else:
        raise SystemExit("unknown scenario %r" % SCENARIO)

    _testcapi.unwatch_dict(wid, d)
    sys.unraisablehook = sys.__unraisablehook__
    print("[main] len now %d" % len(d), flush=True)
    print("[main] survived", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

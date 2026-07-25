"""dict.popitem(): _PyDict_NotifyEvent runs arbitrary Python between reading the
last entry and looking its index up again.

Objects/dictobject.c:dict_popitem_impl

    ep0 = DK_UNICODE_ENTRIES(self->ma_keys);      # :5044  pointer into OLD keys
    i   = self->ma_keys->dk_nentries - 1;         # :5045
    ...
    key = ep0[i].me_key;                          # :5050  borrowed
    _PyDict_NotifyEvent(PyDict_EVENT_DELETED, ..) # :5051  <-- ARBITRARY PYTHON
    hash  = unicode_get_hash(key);                # :5052  key may be freed
    value = ep0[i].me_value;                      # :5053  ep0 may be freed
    STORE_KEY(&ep0[i], NULL);                     # :5054  UAF WRITE
    STORE_VALUE(&ep0[i], NULL);                   # :5055  UAF WRITE

    j = lookdict_index(self->ma_keys, hash, i);   # :5074  NEW keys, STALE i/hash
    assert(j >= 0);                               # :5075  assert-ONLY guard
    dictkeys_set_index(self->ma_keys, j, DKIX_DUMMY);  # :5077  j == -1 on release
    ...
    STORE_KEYS_NENTRIES(self->ma_keys, i);        # :5081  writes Py_EMPTY_KEYS

The Python is reached exactly the way CPY-0096 reaches it: a watcher callback
that returns -1 forces PyErr_FormatUnraisable, which calls sys.unraisablehook.

Expected: debug build -> Assertion `j >= 0' failed (SIGABRT).
          release build -> silent out-of-bounds write into the *immortal*
          empty_keys_struct, then dk_nentries clobbered.
"""

import sys

import _testcapi

fired = []


def main():
    wid = _testcapi.add_dict_watcher(1)  # dict_watch_callback_error -> returns -1

    d = {}
    for n in range(5):
        d["key%d" % n] = n

    def hook(unraisable):
        # Runs from PyErr_FormatUnraisable, i.e. from inside
        # _PyDict_NotifyEvent, i.e. from the middle of dict_popitem_impl.
        if fired:
            return
        fired.append(1)
        print("  [hook] clearing the dict from inside popitem's notify", flush=True)
        d.clear()
        print("  [hook] returning", flush=True)

    sys.unraisablehook = hook

    _testcapi.watch_dict(wid, d)
    print("[main] calling d.popitem()", flush=True)
    item = d.popitem()
    print("[main] survived, popitem() ->", item, flush=True)
    print("[main] d =", d, flush=True)
    # Touch a fresh empty dict: it shares empty_keys_struct with `d`.
    probe = {}
    print("[main] probe empty dict repr:", repr(probe), flush=True)
    print("[main] probe list(probe):", list(probe), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""dict_popitem: a raw `PyDictUnicodeEntry *ep0` into the keys allocation is
cached BEFORE a call that runs arbitrary Python, and written through afterwards.

Objects/dictobject.c:5042-5082

    PyDictUnicodeEntry *ep0 = DK_UNICODE_ENTRIES(self->ma_keys);  # :5043 raw ptr cached
    i = self->ma_keys->dk_nentries - 1;                           # :5044 index cached
    while (i >= 0 && ep0[i].me_value == NULL) { i--; }
    assert(i >= 0);
    key = ep0[i].me_key;                                          # :5050 BORROWED
    _PyDict_NotifyEvent(PyDict_EVENT_DELETED, self, key, NULL);   # :5051 RUNS PYTHON
    hash = unicode_get_hash(key);                                 # :5052 stale key
    value = ep0[i].me_value;                                      # :5053 stale ep0 READ
    STORE_KEY(&ep0[i], NULL);                                     # :5054 stale ep0 WRITE
    STORE_VALUE(&ep0[i], NULL);                                   # :5055 stale ep0 WRITE
    ...
    j = lookdict_index(self->ma_keys, hash, i);                   # :5074 NEW keys, OLD i
    assert(j >= 0);
    dictkeys_set_index(self->ma_keys, j, DKIX_DUMMY);             # :5077 j may be -1
    ...
    STORE_KEYS_NENTRIES(self->ma_keys, i);                        # :5082 stale nentries

`_PyDict_NotifyEvent` -> `_PyDict_SendEvent` (dictobject.c:8298); a dict watcher
callback returning -1 runs `PyErr_FormatUnraisable` (:8314) ->
`sys.unraisablehook` == arbitrary Python.  `d.clear()` from there runs
`clear_lock_held` -> `dictkeys_decref(oldkeys)` -> `free_keys_object`, so `ep0`
dangles and :5054/:5055 are use-after-free WRITEs.

Note :5074-:5077 and :5082 are independently wrong even when the keys object is
merely *replaced* rather than freed: `i` indexes the old table and is applied to
the new one.

Usage:  python dict_popitem_stale_ep0_uaf.py [mode]
        mode "clear"  (default) -- hook calls d.clear()   -> keys object FREED
        mode "resize"            -- hook grows d          -> keys object REPLACED
"""

import sys

import _testcapi


MODE = sys.argv[1] if len(sys.argv) > 1 else "clear"


def main():
    d = {}
    for i in range(200):
        d["k%d" % i] = i

    fired = []

    def hook(unraisable):
        if fired:
            return
        fired.append(1)
        if MODE == "clear":
            # clear_lock_held -> dictkeys_decref(oldkeys) -> free_keys_object
            d.clear()
        else:
            # insertion_resize -> dictresize -> new_keys_object + free_keys_object
            for j in range(4000):
                d["grow%d" % j] = j

    sys.unraisablehook = hook

    wid = _testcapi.add_dict_watcher(1)
    _testcapi.watch_dict(wid, d)

    print("[main] armed, len=%d, mode=%s" % (len(d), MODE), flush=True)
    item = d.popitem()
    print("[main] popitem -> %r" % (item,), flush=True)
    print("[main] len now %d" % len(d), flush=True)

    _testcapi.unwatch_dict(wid, d)
    sys.unraisablehook = sys.__unraisablehook__
    print("[main] survived", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

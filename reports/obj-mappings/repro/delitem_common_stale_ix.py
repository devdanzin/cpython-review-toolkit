"""delitem_common(): the same assert-ONLY guard as dict_popitem_impl, reached
through the other _PyDict_NotifyEvent site.

_PyDict_DelItem_KnownHash_LockHeld, Objects/dictobject.c:

    ix = _Py_dict_lookup(mp, key, hash, &old_value);   # :3030
    ...
    _PyDict_NotifyEvent(PyDict_EVENT_DELETED, ...);    # :3038  <-- ARBITRARY PYTHON
    delitem_common(mp, hash, ix, old_value);           # :3039  ix / old_value STALE

delitem_common, Objects/dictobject.c:

    hashpos = lookdict_index(mp->ma_keys, hash, ix);   # :2962  fallible: DKIX_EMPTY
    assert(hashpos >= 0);                              # :2963  <-- ONLY guard
    ...
    dictkeys_set_index(mp->ma_keys, hashpos, DKIX_DUMMY);   # :2976  hashpos == -1
    ep = &DK_UNICODE_ENTRIES(mp->ma_keys)[ix];              # :2978
    old_key = ep->me_key;                                   # :2979  already NULL
    ...
    Py_DECREF(old_key);                                     # :2990  Py_DECREF(NULL)
    Py_DECREF(old_value);                                   # :2992  second DECREF

The re-entrant Python pops the very key being deleted, so slot `ix` is already
DKIX_DUMMY / me_key == NULL when the outer frame resumes.

Expected: debug   -> Assertion `hashpos >= 0' failed (SIGABRT)
          release -> Py_DECREF(NULL) -> SIGSEGV
"""

import sys

import _testcapi

fired = []


def main():
    wid = _testcapi.add_dict_watcher(1)

    d = {}
    for n in range(6):
        d["key%d" % n] = n

    def hook(unraisable):
        if fired:
            return
        fired.append(1)
        try:
            print("  [hook] re-entrant d.pop('key3') ->", d.pop("key3"), flush=True)
        except KeyError:
            print("  [hook] key already gone", flush=True)

    sys.unraisablehook = hook

    _testcapi.watch_dict(wid, d)
    print("[main] del d['key3']", flush=True)
    del d["key3"]
    print("[main] survived; d =", d, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

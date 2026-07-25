"""Isolates the assert-ONLY guard in dict_popitem_impl.

    j = lookdict_index(self->ma_keys, hash, i);       # Objects/dictobject.c:5074
    assert(j >= 0);                                   # :5075   <-- ONLY guard
    assert(dictkeys_get_index(self->ma_keys, j) == i); # :5076
    dictkeys_set_index(self->ma_keys, j, DKIX_DUMMY);  # :5077  writes indices[j]
    ...
    STORE_KEYS_NENTRIES(self->ma_keys, i);             # :5081

lookdict_index() is fallible: it returns DKIX_EMPTY (-1) when the probe run for
`hash` hits an unused slot without ever seeing `index`.  Between the entry read
at :5050 and this lookup, :5051 runs _PyDict_NotifyEvent -> a watcher callback
-> PyErr_FormatUnraisable -> sys.unraisablehook -> arbitrary Python.

Here the re-entrant Python calls d.popitem() itself.  The inner call marks slot
`i`'s index DKIX_DUMMY and lowers dk_nentries, WITHOUT reallocating ma_keys --
so there is no use-after-free to mask the defect.  When the outer call resumes,
lookdict_index() can no longer find `i` and returns -1.

Expected: debug build   -> Assertion `j >= 0' failed  (SIGABRT)
          release build -> dictkeys_set_index(keys, -1, ...) writes one byte
                           BEFORE dk_indices, i.e. into dk_nentries.
"""

import sys

import _testcapi

fired = []


def main():
    wid = _testcapi.add_dict_watcher(1)  # callback returns -1 -> unraisable

    d = {}
    for n in range(6):
        d["key%d" % n] = n

    def hook(unraisable):
        if fired:
            return
        fired.append(1)
        print("  [hook] re-entrant d.popitem() ->", d.popitem(), flush=True)

    sys.unraisablehook = hook

    _testcapi.watch_dict(wid, d)
    print("[main] outer d.popitem()", flush=True)
    print("[main] outer returned", d.popitem(), flush=True)
    print("[main] d =", d, flush=True)
    print("[main] len(d) =", len(d), flush=True)
    print("[main] list(d) =", list(d), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

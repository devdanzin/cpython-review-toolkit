"""CPY-0096's GIL-arm consequence: insert_split_key re-entrancy corrupts the
shared-keys table on the DEFAULT (GIL) build.

Objects/dictobject.c:1942 insert_split_key

    LOCK_KEYS(keys);                       // :1962  -- expands to NOTHING in the #else arm
    ix = unicodekeys_lookup_unicode(...);  // :1963
    if (ix == DKIX_EMPTY && keys->dk_usable > 0) {   // :1964  READ dk_usable
        ...
        _PyType_Modified_Unlocked(type);             // :1971  RUNS ARBITRARY PYTHON
        Py_ssize_t hashpos = find_empty_slot(keys, hash);        // :1973
        ix = keys->dk_nentries;                                  // :1974
        dictkeys_set_index(keys, hashpos, ix);                   // :1975
        PyDictUnicodeEntry *ep = &DK_UNICODE_ENTRIES(keys)[ix];  // :1976  WRITE at ix
        STORE_SHARED_KEY(ep->me_key, Py_NewRef(key));            // :1977
        split_keys_entry_added(keys);                            // :1978  dk_usable--
    }

The `dk_usable > 0` test at :1964 is separated from the write at :1976-:1978 by a
call that can run arbitrary Python.  A re-entrant insertion that consumes the
remaining usable slots leaves the outer frame writing entry[dk_nentries] past the
end of the allocated entries array, and drives dk_usable negative.

Path to arbitrary Python inside _PyType_Modified_Unlocked (Objects/typeobject.c
:1211-:1225): a type watcher callback that returns -1 makes CPython call
PyErr_FormatUnraisable(...), which dispatches to sys.unraisablehook -- a plain
Python callable.  _testcapi.add_type_watcher(1) installs exactly such a callback.

The FT arm's defence is the keys mutex, which defends against ANOTHER THREAD, not
against RE-ENTRY on this one (it is _Py_LOCK_DONT_DETACH and non-reentrant, so
under free-threading the same input hangs -- that is CPY-0096).  In the #else arm
LOCK_KEYS expands to nothing, so there is no barrier at all and the write lands.

Run:
    release-gil-nojit-asan/python gil_arm_insert_split_key_reentry.py   # ASan report
    debug-gil-nojit/python        gil_arm_insert_split_key_reentry.py   # assertion
    release-gil-nojit/python      gil_arm_insert_split_key_reentry.py   # silent corruption
"""

import sys

import _testcapi


class T:
    """Heap type -> Py_TPFLAGS_INLINE_VALUES + a shared (split) keys table."""


reentered = [False]
inner = []


def hook(unraisable):
    # Runs from PyErr_FormatUnraisable() inside _PyType_Modified_Unlocked(),
    # which insert_split_key called at dictobject.c:1971 -- i.e. after the
    # `keys->dk_usable > 0` test at :1964 and before the entry write at :1976.
    if not reentered[0]:
        reentered[0] = True
        o = T()
        for i in range(40):
            # each one is another insert_split_key on the SAME shared keys object
            setattr(o, f"reent{i}", i)
        inner.append(o)


def main():
    sys.unraisablehook = hook
    wid = _testcapi.add_type_watcher(1)  # kind 1 == callback that returns -1
    _testcapi.watch_type(wid, T)

    seed = T()
    seed.warmup = 0  # materialise CACHED_KEYS(T) and arm the watcher path

    victim = T()
    print("about to trigger re-entrant insert_split_key", flush=True)
    victim.trigger = 1
    print("survived the write", flush=True)

    d = victim.__dict__
    print(f"reentered            = {reentered[0]}")
    print(f"victim.__dict__      = {d}")
    print(f"len(victim.__dict__) = {len(d)}")
    print(f"victim.trigger       = {getattr(victim, 'trigger', '<MISSING>')}")
    print(f"inner obj dict len   = {len(inner[0].__dict__) if inner else 'n/a'}")

    for _ in range(3):
        T().probe = 1
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

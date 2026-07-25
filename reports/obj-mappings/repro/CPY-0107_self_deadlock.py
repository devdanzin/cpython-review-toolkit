"""CPY-0107 reproduction: _Py_dict_lookup:1385 self-deadlocks on the keys mutex.

Objects/dictobject.c:1384-1389 (_Py_dict_lookup), split-keys + non-str key branch:

    INCREF_KEYS_FT(dk);
    LOCK_KEYS_IF_SPLIT(dk, kind);          # -> PyMutex_LockFlags(&dk->dk_mutex,
    ix = unicodekeys_lookup_generic(...);  #        _Py_LOCK_DONT_DETACH)
    UNLOCK_KEYS_IF_SPLIT(dk, kind);

`unicodekeys_lookup_generic` -> do_lookup -> compare_unicode_generic:1168 runs
`PyObject_RichCompareBool(startkey, key, Py_EQ)` -- arbitrary Python -- and
`Py_DECREF(startkey)`:1169, with the shared-keys mutex held.
dictobject.c:219-226 forbids BOTH in so many words.

A PyMutex is NOT reentrant, and `_Py_LOCK_DONT_DETACH` makes the waiter park
WITHOUT detaching its thread state (Python/lock.c:139 passes
`(flags & _PY_LOCK_DETACH) != 0` to _PyParkingLot_Park, and DONT_DETACH == 0).
So one thread that re-enters split-keys dict code from inside its own __eq__
parks on a mutex it already holds -- no second thread, no race, no timing window.

Reaching the locked path matters: on a free-threaded build the READ entry points
(dict_subscript, PyDict_GetItem, ...) go through `_Py_dict_lookup_threadsafe`
(dictobject.c:1599), whose `compare_generic_threadsafe`:1577 runs the very same
RichCompareBool with NO keys lock -- that is the guarded twin.  The MUTATION
entry points (insertdict:2038, _PyDict_DelItem_KnownHash_LockHeld:3030,
dict_setdefault:3291) call `_Py_dict_lookup` directly and do take the lock.
So the trigger is a *store* (or delete), not a lookup.

Expected:
  free-threaded build -> hangs forever inside __eq__ (SIGKILL from `timeout`)
  default GIL build   -> KeyError/normal completion (LOCK_KEYS is empty at :257)
"""

import sys


class C:
    pass


# Populate the type's shared (split) keys with the attribute name "a".
c1 = C()
c1.a = 1
c2 = C()
c2.a = 2

d1 = c1.__dict__  # SPLIT dict over C's cached keys
d2 = c2.__dict__  # same PyDictKeysObject

_reentered = False


class Collide:
    """Non-str key whose hash equals hash('a') -> forces a rich comparison."""

    def __hash__(self):
        return hash("a")

    def __eq__(self, other):
        global _reentered
        if not _reentered:
            _reentered = True
            print("   in __eq__: keys mutex held, re-entering...", flush=True)
            sys.stdout.flush()
            # Second SPLIT-keys mutation on the SAME PyDictKeysObject:
            # insertdict -> _Py_dict_lookup -> LOCK_KEYS_IF_SPLIT ->
            # PyMutex_LockFlags on a mutex this very thread already holds.
            d2[Inert()] = 1
            print("   returned from re-entrant store (NO deadlock)", flush=True)
        return False


class Inert:
    def __hash__(self):
        return hash("a")

    def __eq__(self, other):
        return False


try:
    import _testinternalcapi

    print("d1 split:", _testinternalcapi.has_split_table(d1), flush=True)
except ImportError:
    pass

print("storing into d1 under a hash-colliding non-str key...", flush=True)
d1[Collide()] = 99
print("COMPLETED - no deadlock", flush=True)

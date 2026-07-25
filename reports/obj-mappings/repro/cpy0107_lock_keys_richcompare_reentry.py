"""CPY-0107 -- self-deadlock in _Py_dict_lookup: compare_unicode_generic runs
PyObject_RichCompareBool (arbitrary Python __eq__) while Objects/dictobject.c
holds keys->dk_mutex, a NON-recursive PyMutex taken with _Py_LOCK_DONT_DETACH.

Objects/dictobject.c:218-227 states the rule this violates:

    // gh-151593: The _Py_LOCK_DONT_DETACH flag ensures that the outer critical
    // section is not dropped if there is some contention on the keys lock.
    // It also means that it will be important that LOCK_KEYS() is essentially
    // the "inner-most" code and that we don't call Py_DECREF() or similar while
    // holding the keys lock.

Chain (free-threaded build only; LOCK_KEYS is a no-op under the GIL):

  d.pop(Evil())                # d is a SPLIT instance dict
    -> Objects/dictobject.c:_Py_dict_lookup
         kind == DICT_KEYS_SPLIT, key is not exactly str
         LOCK_KEYS_IF_SPLIT(dk, kind)                    # :1385  raw dk_mutex
         unicodekeys_lookup_generic -> do_lookup
           -> compare_unicode_generic                    # :1157
                PyObject_RichCompareBool(startkey, key)  # :1168 == ARBITRARY PYTHON
                  -> Evil.__eq__
                     -> victim.newattr = 1
                        -> insert_split_key
                           LOCK_KEYS(keys)               # :1962 same keys -> HANG
                Py_DECREF(startkey)                      # :1169 also forbidden

Expected: hangs (SIGALRM fires) on a free-threaded build; completes on GIL.
Run with a hard timeout; the process cannot be interrupted by Ctrl-C once
parked on the mutex.
"""

import signal
import sys


class C:
    """Plain class -> shared/split keys, so the instance dict is split."""


def main():
    seed_a = C()
    seed_a.x = 1
    victim = C()
    victim.x = 2  # shares C's cached split keys with seed_a

    target_hash = hash("x")
    reentered = []

    class Evil:
        def __hash__(self):
            # Collide with the existing unicode key 'x' so that
            # compare_unicode_generic reaches its RichCompareBool arm.
            return target_hash

        def __eq__(self, other):
            print("  [__eq__] entered with other=%r "
                  "(dk_mutex is HELD here)" % (other,), flush=True)
            if reentered:
                return False
            reentered.append(1)
            # New attribute name -> new split-key slot -> insert_split_key,
            # which does LOCK_KEYS() on the *same* keys object.
            victim.second_attribute = 2
            print("  [__eq__] re-entry returned (NO deadlock)", flush=True)
            return False

    d = seed_a.__dict__  # materialised, still a split table
    print("[main] dict=%r  keys shared with victim=%r" % (d, victim.__dict__),
          flush=True)

    signal.alarm(3)
    print("[main] calling d.pop(Evil()) ...", flush=True)
    d.pop(Evil(), None)
    signal.alarm(0)
    print("[main] completed without deadlock; __eq__ ran: %s"
          % bool(reentered), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

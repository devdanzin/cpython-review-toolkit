"""Self-deadlock in insert_split_key: _PyType_Modified_Unlocked runs arbitrary
Python (sys.unraisablehook) while Objects/dictobject.c holds keys->dk_mutex,
a NON-recursive PyMutex taken with _Py_LOCK_DONT_DETACH.

Chain (free-threaded build only; LOCK_KEYS is a no-op under the GIL):

  c.x = 1
    -> _PyObject_StoreInstanceAttribute
    -> Objects/dictobject.c:insert_split_key
         Py_BEGIN_CRITICAL_SECTION_MUTEX(TYPE_LOCK)      # :1959
         LOCK_KEYS(keys)                                 # :1962  raw dk_mutex, DONT_DETACH
         _PyType_Modified_Unlocked(type)                 # :1971
           -> Objects/typeobject.c:1222 watcher cb returns -1
           -> Objects/typeobject.c:1223 PyErr_FormatUnraisable
              -> sys.unraisablehook  == ARBITRARY PYTHON
                 -> c2.y = 2
                    -> insert_split_key (re-entered, same keys)
                       LOCK_KEYS(keys)  <-- already held, non-recursive -> HANG

Expected: hangs (SIGALRM fires) on a free-threaded build.
Run with a hard timeout; the process cannot be interrupted by Ctrl-C once
parked on the mutex.
"""

import sys

import _testcapi


class C:
    """Plain class -> shared/split keys, so insert_split_key is used."""


def main():
    # kind=1 -> type_modified_callback_error, which returns -1 with an
    # exception set, forcing typeobject.c:1223 PyErr_FormatUnraisable.
    wid = _testcapi.add_type_watcher(1)
    _testcapi.watch_type(wid, C)

    victim = C()
    reentered = []

    def hook(unraisable):
        # This runs from PyErr_FormatUnraisable, i.e. from inside
        # _PyType_Modified_Unlocked, i.e. with keys->dk_mutex HELD.
        if reentered:
            return
        reentered.append(1)
        print("  [hook] running with dk_mutex held; re-entering insert_split_key",
              flush=True)
        # New attribute name -> new split-key slot -> insert_split_key again.
        victim.second_attribute = 2
        print("  [hook] returned (NO deadlock)", flush=True)

    sys.unraisablehook = hook

    obj = C()
    print("[main] assigning first attribute (arms the watcher)...", flush=True)
    obj.first_attribute = 1
    print("[main] completed without deadlock", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

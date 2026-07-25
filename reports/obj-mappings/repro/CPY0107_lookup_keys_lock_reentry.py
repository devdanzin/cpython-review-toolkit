"""CPY-0107 dynamic test: _Py_dict_lookup runs a user __eq__ under LOCK_KEYS.

    _Py_dict_lookup(mp, key, hash, value_addr)          # dictobject.c:1341
        if (kind != DICT_KEYS_GENERAL) {
            if (PyUnicode_CheckExact(key)) { ... }      # :1371  fast path
            else {
                INCREF_KEYS_FT(dk);
                LOCK_KEYS_IF_SPLIT(dk, kind);           # :1385  raw dk_mutex, DONT_DETACH
                ix = unicodekeys_lookup_generic(...)    # :1387
                    -> compare_unicode_generic          # dictobject.c:1156
                         PyObject_RichCompareBool(...)  # :1168  == ARBITRARY PYTHON
                UNLOCK_KEYS_IF_SPLIT(dk, kind);         # :1389
            }
        }

dictobject.c:218-227 forbids exactly this:

    "We are not allowed to acquire other locks within LOCK_KEYS(). ... it will be
     important that LOCK_KEYS() is essentially the 'inner-most' code"

A NON-exact-unicode key (a `str` subclass) looked up in a SPLIT dict therefore runs
user Python with the keys mutex held.  Anything that user code does which needs the
same keys mutex -- setting a new attribute on any instance of the owning class, i.e.
insert_split_key (dictobject.c:1962) -- parks forever, because dk_mutex is a plain
non-recursive PyMutex taken with _Py_LOCK_DONT_DETACH.

Free-threaded builds only: LOCK_KEYS_IF_SPLIT is a no-op under the GIL.
Single-threaded -- no concurrency required, only re-entrancy.

NOTE on the entry point: `dict.get` / `d[k]` reads go through
`_Py_dict_lookup_threadsafe` (dictobject.c:1601), which uses
`unicodekeys_lookup_generic_threadsafe` and takes NO keys lock.  Only the WRITE
paths reach `_Py_dict_lookup` proper -- `insertdict` (dictobject.c:2036) and
`delitem_common` fall through to it whenever the key is not exact-unicode.  So the
trigger is `d[SubStr(...)] = v` / `del d[SubStr(...)]`, not `d.get(SubStr(...))`.
Measured: the .get() form does NOT deadlock (4/4), the assignment form does.

Expected: HANG (timeout) on *-ft-* builds, clean exit on *-gil-* builds.

Usage:  PYTHON_GIL=0 python CPY0107_lookup_keys_lock_reentry.py [mode]
        mode = setattr (default) | setitem | get
"""

import os
import signal
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "setattr"

# Instrumentation hook: HANG_ALARM=<seconds> arms a SIGALRM so the process can be
# caught in a debugger.  ptrace_scope blocks attaching to a running process here, so
# the way to get a backtrace is
#   HANG_ALARM=5 gdb -batch -ex run -ex 'thread apply all bt 25' --args <python> <this>
# Off by default; does not affect the deadlock.
if os.environ.get("HANG_ALARM"):
    signal.alarm(int(os.environ["HANG_ALARM"]))


class C:
    pass


owner = C()
owner.a = 1          # split table: ma_keys is C's shared keys, ma_values inline
d = owner.__dict__   # materialised, still a SPLIT table

# A *different* instance of the same class: same shared keys (same dk_mutex),
# different per-object critical section -- so the only lock in common is the
# keys mutex, and a hang can only be that.
other = C()
other.a = 1
other_d = other.__dict__

state = {"reentered": False}


class SubStr(str):
    """A non-exact-unicode key: forces _Py_dict_lookup down the :1385 arm."""

    def __hash__(self):
        return str.__hash__(self)

    def __eq__(self, other_key):
        # Runs from compare_unicode_generic (dictobject.c:1168) with
        # keys->dk_mutex HELD (_Py_LOCK_DONT_DETACH).
        if not state["reentered"]:
            state["reentered"] = True
            print("  [__eq__] running with dk_mutex held; re-entering ...", flush=True)
            if MODE == "get":
                other_d.get(SubStr("a"))
            elif MODE == "setitem":
                # insertdict -> insert_split_key -> LOCK_KEYS(same keys) -> HANG
                other_d["brand_new_attribute"] = 2
            else:
                # _PyObject_StoreInstanceAttribute -> insert_split_key -> LOCK_KEYS
                other.brand_new_attribute = 2
            print("  [__eq__] returned (NO deadlock)", flush=True)
        return str.__eq__(self, other_key)


def main():
    print("[main] mode=%s  gil=%s"
          % (MODE, getattr(sys, "_is_gil_enabled", lambda: "n/a")()), flush=True)
    print("[main] split table: %r" % (list(d),), flush=True)
    print("[main] assigning through a str-subclass key (insertdict -> "
          "_Py_dict_lookup:1385) ...", flush=True)
    d[SubStr("a")] = 99
    print("[main] completed without deadlock; d=%r, reentered=%s"
          % (dict(d), state["reentered"]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""obj-mappings / refcount-auditor

dict_popitem_impl (Objects/dictobject.c:5043-5055) captures `ep0` (the entries
array of self->ma_keys) and `key = ep0[i].me_key` BEFORE calling
_PyDict_NotifyEvent(PyDict_EVENT_DELETED, ...) at :5051, then dereferences both
AFTER it at :5052-5055.

_PyDict_SendEvent (Objects/dictobject.c:8298) runs PyErr_FormatUnraisable() when
a watcher callback returns -1 -- i.e. CPython itself executes sys.unraisablehook,
arbitrary Python, from inside the notification.  The dict-watcher contract
(Doc/c-api/dict.rst:582) only forbids the *callback* from running Python; it says
nothing about the error reporting CPython performs after the callback returns.

Clearing the dict from the hook frees the keys object, so :5053 is a
heap-use-after-free READ and :5054 (STORE_KEY) a heap-use-after-free WRITE.

Run on a GIL ASan build:
  ~/projects/python_build_matrix/builds/debug-gil-nojit-asan/python <thisfile>
"""

import sys

import _testcapi

# kind==1 -> dict_watch_callback_error: sets RuntimeError and returns -1.
# The callback itself is fully contract-conforming: it does not touch the dict
# and does not execute Python.
WATCHER = _testcapi.add_dict_watcher(1)

d = {}
for i in range(64):
    # non-interned keys so that the strings really die when the dict is cleared
    d["key_%d" % i + "!"] = ["value", i]

_testcapi.watch_dict(WATCHER, d)

_fired = False


def hook(unraisable):
    global _fired
    if _fired:
        return
    _fired = True
    # Re-entrant mutation of the very dict whose popitem() is in progress.
    # clear() frees the (malloc'd) PyDictKeysObject that dict_popitem_impl
    # still holds in its local `ep0`.
    d.clear()


sys.unraisablehook = hook

print("before popitem", flush=True)
d.popitem()
print("after popitem (no crash)", flush=True)

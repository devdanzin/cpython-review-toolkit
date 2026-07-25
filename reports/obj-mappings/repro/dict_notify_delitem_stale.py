"""obj-mappings / refcount-auditor

_PyDict_DelItem_KnownHash_LockHeld (Objects/dictobject.c:3030-3039) obtains `ix`
and the BORROWED `old_value` from _Py_dict_lookup at :3030, then calls
_PyDict_NotifyEvent(PyDict_EVENT_DELETED, ...) at :3038 and passes both stale
values to delitem_common() at :3039.

_PyDict_SendEvent runs PyErr_FormatUnraisable() -- sys.unraisablehook, arbitrary
Python -- when the watcher callback returns -1.  Clearing the dict there makes
delitem_common:
  * call lookdict_index() on Py_EMPTY_KEYS, which returns DKIX_EMPTY (-1),
    tripping `assert(hashpos >= 0)` at :2963 (SIGABRT on a debug build) and
    writing indices[-1] with NDEBUG,
  * Py_DECREF(old_value) at :2992 on a value the clear already freed.

Run on a GIL ASan build:
  ~/projects/python_build_matrix/builds/debug-gil-nojit-asan/python <thisfile>
"""

import sys

import _testcapi

WATCHER = _testcapi.add_dict_watcher(1)  # error callback: RuntimeError, return -1

d = {}
for i in range(64):
    d["key_%d" % i + "!"] = ["value", i]

_testcapi.watch_dict(WATCHER, d)

_fired = False


def hook(unraisable):
    global _fired
    if _fired:
        return
    _fired = True
    d.clear()


sys.unraisablehook = hook

print("before delitem", flush=True)
del d["key_0!"]
print("after delitem (no crash)", flush=True)

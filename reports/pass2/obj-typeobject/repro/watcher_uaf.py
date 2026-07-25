"""Re-entrancy through the type-watcher callback invalidates the live
PyDict_Next cursor over tp_subclasses in _PyType_Modified_Unlocked.

Objects/typeobject.c:1193-1207

    PyObject *subclasses = lookup_tp_subclasses(type);   // BORROWED
    ...
    while (PyDict_Next(subclasses, &i, NULL, &ref)) {
        PyTypeObject *subclass = type_from_ref(ref);
        ...
        _PyType_Modified_Unlocked(subclass);   // fires SUBCLASS's watchers -> user Python
        Py_DECREF(subclass);
    }

The recursive call fires the *subclass's* watchers.  A watcher that returns -1
reaches PyErr_FormatUnraisable (:1223) -> sys.unraisablehook -> arbitrary Python,
executed while `subclasses` is a borrowed pointer and `i` is a live cursor.

If that Python reparents the subclass (D.__bases__ = (object,)), CPython runs
    remove_all_subclasses -> remove_subclass(C, D) -> PyDict_DelItem(subclasses)
and, when the dict becomes empty,
    clear_tp_subclasses(C) -> Py_CLEAR(C->tp_subclasses)
which FREES the dict the outer loop is still iterating.
"""
import sys

import _testcapi


class C:
    pass


class D(C):
    pass


# Give both types a non-zero tp_version_tag, otherwise
# _PyType_Modified_Unlocked early-returns at :1188 and never reaches the loop.
C.probe = 1
D.probe2 = 1
assert C.probe == 1
assert D.probe2 == 1

fired = []


def hook(args):
    if fired:
        return
    fired.append(args.err_msg)
    # Runs inside _PyType_Modified_Unlocked(D), which is inside
    # _PyType_Modified_Unlocked(C)'s PyDict_Next loop over C.tp_subclasses.
    try:
        D.__bases__ = (object,)
        fired.append("reparented")
    except Exception as exc:  # noqa: BLE001
        fired.append("reparent-failed: %r" % (exc,))


wid = _testcapi.add_type_watcher(1)  # error watcher: raises + returns -1
_testcapi.watch_type(wid, D)
sys.unraisablehook = hook

C.trigger = 2  # -> _PyType_Modified_Unlocked(C) -> recurse into D -> watcher -> hook

sys.unraisablehook = sys.__unraisablehook__
print("hook:", fired)
print("C subclasses now:", C.__subclasses__())
print("survived")

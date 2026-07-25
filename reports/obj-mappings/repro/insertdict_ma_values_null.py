"""insertdict(): mp->ma_values is dereferenced after insert_split_key() ran
arbitrary Python that turned the dict into a combined table.

Objects/dictobject.c:insertdict

    if (_PyDict_HasSplitTable(mp) && PyUnicode_CheckExact(key)) {
        ix = insert_split_key(mp->ma_keys, key, hash);   # :2028  <-- RUNS PYTHON
        if (ix != DKIX_EMPTY) {
            _PyDict_InsertSplitValue(mp, key, value, ix);# :2030
            ...

Objects/dictobject.c:_PyDict_InsertSplitValue

    PyObject *old_value = mp->ma_values->values[ix];     # :1995  ma_values == NULL

insert_split_key (:1971) calls _PyType_Modified_Unlocked(), which runs a type
watcher callback and sys.unraisablehook -- this is CPY-0096's mechanism, and
the LOCK_KEYS() region it happens in is NOT #ifdef Py_GIL_DISABLED, so the
default GIL build reaches it too.

The re-entrant Python stores a non-unicode key, which forces
insert_combined_dict -> insertion_resize -> dictresize -> set_values(mp, NULL).

_PyDict_HasSplitTable() was tested at :2027, BEFORE the call that invalidates it.

Expected: SIGSEGV reading NULL->values[ix] on every build.
"""

import sys

import _testcapi

fired = []


class C:
    pass


def main():
    obj = C()
    obj.first = 1
    d = obj.__dict__  # materialize the split dict (ma_values == inline values)

    def hook(unraisable):
        # Runs from PyErr_FormatUnraisable inside _PyType_Modified_Unlocked,
        # i.e. from the middle of insert_split_key, i.e. from the middle of
        # insertdict.
        if fired:
            return
        fired.append(1)
        print("  [hook] d[1] = 1  -> dictresize -> ma_values = NULL", flush=True)
        d[1] = 1
        print("  [hook] ma_values is now NULL; returning into insertdict", flush=True)

    sys.unraisablehook = hook

    wid = _testcapi.add_type_watcher(1)  # type_modified_callback_error
    _testcapi.watch_type(wid, C)

    print("[main] d['second'] = 2", flush=True)
    d["second"] = 2
    print("[main] survived; d =", d, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""dict.setdefault(): the same ma_values NULL deref, one function over.

Objects/dictobject.c:dict_setdefault_ref_lock_held

    if (_PyDict_HasSplitTable(mp) && PyUnicode_CheckExact(key)) {  # :4857 test
        ix = insert_split_key(mp->ma_keys, key, hash);             # :4858 RUNS PYTHON
        if (ix != DKIX_EMPTY) {
            PyObject *value = mp->ma_values->values[ix];           # :4859 ma_values NULL
            ...
            _PyDict_InsertSplitValue(mp, key, default_value, ix);  # :4864

Identical to insertdict_ma_values_null.py: the split-table test at :4857 happens
before insert_split_key runs the type watcher / sys.unraisablehook that turns
the dict into a combined table.

Expected: SIGSEGV on every build.
"""

import sys

import _testcapi

fired = []


class C:
    pass


def main():
    obj = C()
    obj.first = 1
    d = obj.__dict__  # split dict, ma_values == inline values

    def hook(unraisable):
        if fired:
            return
        fired.append(1)
        print("  [hook] d[1] = 1  -> dictresize -> ma_values = NULL", flush=True)
        d[1] = 1
        print("  [hook] returning into dict_setdefault_ref_lock_held", flush=True)

    sys.unraisablehook = hook

    wid = _testcapi.add_type_watcher(1)
    _testcapi.watch_type(wid, C)

    print("[main] d.setdefault('second', 2)", flush=True)
    d.setdefault("second", 2)
    print("[main] survived; d =", d, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

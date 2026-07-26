"""notify sites :1997 (ADDED) and :2003 (MODIFIED) -- _PyDict_InsertSplitValue.

Objects/dictobject.c:1989-2010

    1995:  PyObject *old_value = mp->ma_values->values[ix];   <-- borrowed, pre-notify
    1996:  if (old_value == NULL) {
    1997:      _PyDict_NotifyEvent(PyDict_EVENT_ADDED, ...)    <-- window
    1998:      STORE_SPLIT_VALUE(mp, ix, Py_NewRef(value));    <-- mp->ma_values->values[ix]
    1999:      _PyDictValues_AddToInsertionOrder(mp->ma_values, ix);
    2000:      STORE_USED(mp, mp->ma_used + 1);
    2002:  else {
    2003:      _PyDict_NotifyEvent(PyDict_EVENT_MODIFIED, ...)  <-- window
    2004:      STORE_SPLIT_VALUE(mp, ix, Py_NewRef(value));
    2007:      Py_DECREF(old_value);                            <-- stale borrowed ref

`ix` also comes from insert_split_key() at :2028, before either notify.

Reaching a split table from Python: `d = obj.__dict__` of an instance of a
plain class is a *split* dict whose ma_values aliases the object's inline
values.  `d[key] = v` then goes insertdict -> insert_split_key ->
_PyDict_InsertSplitValue.

Modes:

  mod_clear     :2003.  Values are embedded (obj alive).  d.clear() takes the
                clear_embedded_values() branch, which DECREFs every value.
                old_value hits 0; :2007 DECREFs freed memory.

  mod_detached  :2003.  `del obj` first, so detach_dict_from_object() gives the
                dict a *heap* values array with embedded == 0.  d.clear() then
                takes the :3154 branch -> set_values(mp, NULL).  Back at :2004
                STORE_SPLIT_VALUE dereferences a NULL ma_values.

  add_detached  :1997, same NULL ma_values, via the ADDED branch (a shared key
                that has no value yet).

Usage:  python notify_site_1997_2003_insert_split_value.py [mode]
"""

import sys

import _testcapi

MODE = sys.argv[1] if len(sys.argv) > 1 else "mod_clear"


class C:
    pass


def main():
    o = C()
    # Populate the shared-keys table with a,b,c; leave `c` unset on the
    # instance that we will actually poke, so the ADDED branch is reachable.
    warm = C()
    warm.a = 1
    warm.b = 2
    warm.c = 3
    del warm

    o.a = ["a"]
    o.b = ["b"]
    if MODE.startswith("mod"):
        o.c = ["c"]

    d = o.__dict__  # split dict: ma_values aliases o's inline values

    if MODE.endswith("detached"):
        del o  # -> PyObject_ClearManagedDict -> detach: heap values, embedded=0

    fired = []

    def hook(unraisable):
        if fired:
            return
        fired.append(1)
        d.clear()

    sys.unraisablehook = hook
    wid = _testcapi.add_dict_watcher(1)
    _testcapi.watch_dict(wid, d)
    print("[main] armed mode=%s d=%r" % (MODE, d), flush=True)

    if MODE.startswith("mod"):
        d["c"] = ["replacement"]  # MODIFIED -> :2003
    else:
        d["c"] = ["fresh"]  # ADDED (shared key, no value) -> :1997

    print("[main] returned from _PyDict_InsertSplitValue", flush=True)
    _testcapi.unwatch_dict(wid, d)
    sys.unraisablehook = sys.__unraisablehook__
    print("[main] len=%d keys=%r" % (len(d), sorted(d)), flush=True)
    real = len(list(d.keys()))
    if len(d) != real:
        print("[main] *** ma_used=%d real entries=%d ***" % (len(d), real), flush=True)
    print("[main] items=%r" % (sorted(d.items()),), flush=True)
    print("[main] survived", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""delete_index_from_values(): a search loop whose only bound is an assert().

Objects/dictobject.c:delete_index_from_values

    uint8_t *array = get_insertion_order_array(values);
    int size = values->size;
    assert(size <= values->capacity);                   # :2941
    int i;
    for (i = 0; array[i] != ix; i++) {                  # :2943  <-- unbounded
        assert(i < size);                               # :2944  <-- ONLY bound
    }
    assert(i < size);                                   # :2946
    size--;
    for (; i < size; i++) {
        array[i] = array[i+1];                          # :2949  OOB WRITE
    }

Reached from delitem_common:2971 on a split table.  delitem_common is called
from _PyDict_DelItem_KnownHash_LockHeld:3039, one line after
_PyDict_NotifyEvent(PyDict_EVENT_DELETED, ...) at :3038 -- which runs a watcher
callback and sys.unraisablehook, i.e. arbitrary Python.

If that Python removes the same key, index `ix` is no longer in the insertion
order array when the outer frame resumes.  On a debug build assert(i < size)
fires.  On a release build the loop scans past the end of the (16-byte-max)
order array for a byte equal to `ix`, then memmoves over whatever it found.

Expected: debug   -> Assertion `i < size' failed (SIGABRT)
          release -> out-of-bounds read + write on the values allocation
                     (ASan: heap-buffer-overflow), ma_used driven negative
"""

import sys

import _testcapi

fired = []


class C:
    pass


def main():
    obj = C()
    obj.a = 1
    obj.b = 2
    obj.c = 3
    d = obj.__dict__  # split table: ma_values is the inline values array

    def hook(unraisable):
        if fired:
            return
        fired.append(1)
        try:
            print("  [hook] re-entrant d.pop('b') ->", d.pop("b"), flush=True)
        except KeyError:
            print("  [hook] already gone", flush=True)

    sys.unraisablehook = hook

    wid = _testcapi.add_dict_watcher(1)  # callback returns -1 -> unraisable
    _testcapi.watch_dict(wid, d)

    print("[main] del d['b']", flush=True)
    del d["b"]
    print("[main] survived; d =", d, "  len =", len(d), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

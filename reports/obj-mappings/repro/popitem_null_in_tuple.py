"""dict.popitem() hands Python a tuple containing a raw C NULL.

Same re-entry as popitem_assert_j_negative.py, but every key string is kept
alive in `keepalive`, so the use-after-free read at Objects/dictobject.c:5052
cannot fire and the defect is visible in its pure form:

    STORE_VALUE(&ep0[i], NULL);        # :5055  the inner popitem already did this
    ...
    j = lookdict_index(...); assert(j >= 0);   # :5074-5075 assert-ONLY guard
    PyTuple_SET_ITEM(res, 0, key);     # :5079
    PyTuple_SET_ITEM(res, 1, value);   # :5080  value == NULL

`value` was read at :5053 *after* :5051 ran arbitrary Python that already
emptied the slot, so the 2-tuple returned to Python has ob_item[1] == NULL.
repr() of that tuple is safe (PyObject_Repr special-cases NULL), but ANY real
use -- t[1], iteration, unpacking -- runs Py_INCREF(NULL).

Expected: SIGSEGV at t[1] on every build.
"""

import sys

import _testcapi

fired = []


def main():
    wid = _testcapi.add_dict_watcher(1)

    d = {}
    for n in range(6):
        d["key%d" % n] = n
    keepalive = list(d.keys()) + list(d.values())  # nothing gets freed

    def hook(unraisable):
        if fired:
            return
        fired.append(1)
        d.popitem()

    sys.unraisablehook = hook

    _testcapi.watch_dict(wid, d)
    t = d.popitem()
    print("[main] popitem() ->", t, flush=True)
    print("[main] len(t) =", len(t), flush=True)
    print("[main] about to evaluate t[1] (Py_INCREF of the NULL slot)", flush=True)
    x = t[1]
    print("[main] survived; t[1] =", x, flush=True)
    print("[main] keepalive holds", len(keepalive), "objects", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

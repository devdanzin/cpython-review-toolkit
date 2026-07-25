"""CPY-0130 -- the plain `mp->ma_values = values;` at Objects/dictobject.c:7976
publishes a freshly-allocated values array into a LIVE, SHARED, lock-free-readable
dict.

    Objects/dictobject.c:7953-7983  detach_dict_from_object
        PyDictValues *values = copy_values(mp->ma_values);   /* :7970 */
        if (values == NULL) { ... }
        mp->ma_values = values;                              /* :7976  PLAIN STORE */
        invalidate_and_clear_inline_values(_PyObject_InlineValues(obj));  /* :7978 */

Everywhere else in the file the field is published through `set_values()`
(:215), which is `_Py_atomic_store_ptr_release`.  The lock-free readers pair
against exactly that release store:

    Objects/dictobject.c:6086  dictiter_iternext_threadsafe
        PyDictValues *values = _Py_atomic_load_ptr_consume(&d->ma_values);
    Objects/dictobject.c:1626  _Py_dict_lookup_threadsafe
        ... _Py_atomic_load_ptr(&mp->ma_values) ...

`ASSERT_WORLD_STOPPED_OR_OBJ_LOCKED(mp)` at :7965 excludes other *writers*, not
the lock-free *readers*, which take nothing.

REACHABILITY (the question this script exists to answer).  On the free-threaded
build the call chain to :7976 with a live, externally-referenced dict is:

  route 1   obj.__dict__ = X
            _PyObject_SetManagedDict:7900  (Py_BEGIN_CRITICAL_SECTION2(obj, prev_dict))
              -> replace_dict_probably_inline_materialized:7846
                 -> _PyDict_DetachFromObject -> detach_dict_from_object:7976

  route 2   obj.__class__ = Y
            Objects/typeobject.c:7778-7780 (Py_BEGIN_CRITICAL_SECTION(dict))
                 -> _PyDict_DetachFromObject -> detach_dict_from_object:7976

Both require `mp->ma_values == _PyObject_InlineValues(obj)` (:7959), i.e. a dict
that has been MATERIALIZED but not yet detached -- precisely what
`d = obj.__dict__` hands to Python.  `d` is an ordinary strong reference held by
any other thread, so the dict is neither uniquely referenced nor private.

(The third caller, PyObject_ClearManagedDict:8007, is the one whose comment says
"known to have no references"; that is the object, not the dict, and it is also
the only path to the sibling plain store at :8019 -- which is additionally gated
on a `copy_values` OOM.  See the report.)

Usage:  <ft-python> CPY-0130_ma_values_plain_store.py [rounds] [nreaders] [mode]
        mode = dict | class | both   (default both)
Expected primary evidence is a TSan report pairing
`detach_dict_from_object dictobject.c:7976` with a `_Py_atomic_load_*` reader.
"""

import sys
import threading

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
NREADERS = int(sys.argv[2]) if len(sys.argv) > 2 else 4
MODE = sys.argv[3] if len(sys.argv) > 3 else "both"

POOL = 24
NATTR = 12
KEYS = ["a%d" % i for i in range(NATTR)]
STOP = False
SLOT = [None] * POOL


class C:
    pass


class D:
    pass


def fresh():
    o = C()
    for i, k in enumerate(KEYS):
        setattr(o, k, i)
    d = o.__dict__          # materialise: ma_values still == inline values
    return (o, d)


def reader():
    while not STOP:
        for i in range(POOL):
            pair = SLOT[i]
            if pair is None:
                continue
            d = pair[1]
            try:
                for k in d:               # dictiter_iternext_threadsafe:6086
                    pass
                for k in KEYS:
                    d.get(k)              # _Py_dict_lookup_threadsafe:1626
                len(d)
                list(d.values())
            except Exception:
                pass


def writer(kind):
    i = 0
    while not STOP:
        pair = SLOT[i]
        if pair is not None:
            o = pair[0]
            try:
                if kind == "dict":
                    o.__dict__ = {"z": 1}
                else:
                    o.__class__ = D
            except Exception:
                pass
        SLOT[i] = fresh()
        i = (i + 1) % POOL


def main():
    global STOP
    print("rounds=%d readers=%d mode=%s gil=%s"
          % (ROUNDS, NREADERS, MODE,
             getattr(sys, "_is_gil_enabled", lambda: "n/a")()), flush=True)
    for i in range(POOL):
        SLOT[i] = fresh()

    ts = [threading.Thread(target=reader, daemon=True) for _ in range(NREADERS)]
    kinds = {"dict": ["dict"], "class": ["class"], "both": ["dict", "class"]}[MODE]
    for k in kinds:
        ts.append(threading.Thread(target=writer, args=(k,), daemon=True))
    for t in ts:
        t.start()

    for r in range(ROUNDS):
        for i in range(POOL):
            SLOT[i] = fresh()
        if r % 500 == 0:
            print("round", r, flush=True)

    STOP = True
    for t in ts:
        t.join(timeout=10.0)
    print("survived %d rounds" % ROUNDS, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

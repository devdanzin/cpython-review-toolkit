"""Dual-build divergence: dictiter_iternext_threadsafe leaks key+value on the
'dictionary keys changed during iteration' path.

Objects/dictobject.c:

  FT arm  (dictiter_iternext_threadsafe, :6062-:6167)
      ... acquire_key_value(...)  <-- INCREFs *out_key and *out_value  (:6102/:6120/:6137)
      if ((len = ...di->len) == 0) goto concurrent_modification;       (:6145)
    concurrent_modification: PyErr_SetString(RuntimeError, ...)        (:6153)
    fail: di->di_dict = NULL; Py_DECREF(d); return -1;                 (:6157)
      -> the two references acquired above are never released.

  GIL arm (dictiter_iternextitem_lock_held, :5957-:6031)
      if (di->len == 0) goto fail;                                     (:6012)
      ... *out_key = Py_NewRef(key); *out_value = Py_NewRef(value);    (:6019-:6024)
      -> the check happens BEFORE the increfs, so nothing leaks.

Reachability: `di->len == 0` while an entry is still visible is reachable from
pure Python by mutating the dict during iteration so that ma_used is restored
(defeating the di_used size check) while dk_nentries grows.

Run: <build>/python ft_dictiter_len0_refleak.py
Exit 0 = no leak (expected on the GIL build), exit 1 = leak (FT build).
"""

import gc
import sys
import weakref


class Val:
    """Weak-referenceable, GC-tracked value object."""


def trigger(iterator_factory, want_key, want_value):
    """Drive one dict iterator into the len==0 'keys changed' path.

    Returns (key_ref, value_ref) weakrefs to the objects that were live in the
    entry the iterator found on the failing step.
    """
    v = Val()
    k = Val()
    vref = weakref.ref(v)
    kref = weakref.ref(k)

    a, b = Val(), Val()
    d = {a: 1, b: 2}
    it = iterator_factory(d)

    next(it)          # consumes entry 0 -> di_pos=1, di->len=1
    del d[a]          # ma_used 2 -> 1, entry 0 me_value = NULL, index = DKIX_DUMMY
    d[k] = v          # ma_used back to 2 (di_used check passes), dk_nentries 2 -> 3

    next(it)          # entry 1 (b) -> di_pos=2, di->len=0

    try:
        next(it)      # entry 2 (k/v) found, but di->len == 0
    except RuntimeError as exc:
        assert "changed during iteration" in str(exc), exc
    else:
        raise AssertionError("expected RuntimeError on the third next()")

    del v, k, a, b, d, it
    gc.collect()
    gc.collect()
    return (kref if want_key else None), (vref if want_value else None)


CASES = [
    ("dict.items()", lambda d: iter(d.items()), True, True),
    ("dict.keys()", lambda d: iter(d.keys()), True, False),
    ("dict.values()", lambda d: iter(d.values()), False, True),
]


def main():
    ft = bool(getattr(sys, "_is_gil_enabled", None)) and not sys._is_gil_enabled()
    print(f"build: {sys.version.split()[0]}  free-threaded={ft}")
    leaked = 0
    for name, factory, want_key, want_value in CASES:
        kref, vref = trigger(factory, want_key, want_value)
        bits = []
        if kref is not None:
            alive = kref() is not None
            bits.append(f"key={'LEAKED' if alive else 'freed'}")
            leaked += alive
        if vref is not None:
            alive = vref() is not None
            bits.append(f"value={'LEAKED' if alive else 'freed'}")
            leaked += alive
        print(f"  {name:<16} {' '.join(bits)}")
    print(f"total leaked objects: {leaked}")
    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(main())

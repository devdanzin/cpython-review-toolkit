"""Error-path agent, slice obj-sequences.

Shape: an UNNARROWED `PyErr_Format(PyExc_TypeError, "can't concat ...")` on the
failure path of `PyObject_GetBuffer(user_object, ...)`, which runs a PEP 688
`__buffer__` and can therefore raise ANYTHING.  The reformat overwrites the
user's exception with a TypeError (and, because _PyErr_SetObject chains, buries
it in __context__).

Unguarded sites in the slice:
  Objects/bytesobject.c:1551      _PyBytes_Concat            `bytes + x`
  Objects/bytesobject.c:3291      PyBytes_Concat             C API (_io)
  Objects/bytearrayobject.c:314   PyByteArray_Concat         `bytearray + x`
  Objects/bytearrayobject.c:357   bytearray_iconcat_lock_held `bytearray += x`

Guarded twins, same slice:
  Objects/bytearrayobject.c:1062  bytearray___init___impl   ExceptionMatches
  Objects/bytes_methods.c:697     _Py_bytes_tailmatch       ExceptionMatches

Usage:  <python> errpath_concat_exception_clobber.py
"""

import sys


class Raiser:
    """PEP 688 exporter whose __buffer__ raises a non-TypeError."""

    def __init__(self, exc=KeyboardInterrupt):
        self.exc = exc

    def __buffer__(self, flags):
        raise self.exc("from __buffer__")

    def __release_buffer__(self, view):
        pass


def _check(name, fn):
    try:
        r = fn()
    except BaseException as exc:  # noqa: BLE001 - probe
        ctx = type(exc.__context__).__name__ if exc.__context__ else None
        print(f"PROBE:{name}={type(exc).__name__} context={ctx}", flush=True)
        return
    print(f"PROBE:{name}=NO_EXCEPTION returned={r!r}", flush=True)


def _iadd(target, other):
    target += other
    return target


def main():
    # --- unguarded sites -------------------------------------------------
    _check("bytes_add", lambda: b"abc" + Raiser())
    _check("bytearray_add", lambda: bytearray(b"abc") + Raiser())
    _check("bytearray_iadd", lambda: _iadd(bytearray(b"abc"), Raiser()))
    _check("bytes_join", lambda: b"".join([b"a", Raiser()]))

    # --- guarded twins ---------------------------------------------------
    _check("twin_bytearray_init", lambda: bytearray(Raiser()))
    _check("twin_startswith", lambda: b"abc".startswith(Raiser()))
    _check("twin_endswith", lambda: bytearray(b"abc").endswith(Raiser()))

    # --- other buffer-consuming entry points in the slice ----------------
    _check("bytearray_setslice", lambda: _setslice(Raiser()))
    _check("bytearray_extend", lambda: bytearray(b"abc").extend(Raiser()))
    _check("bytearray_replace", lambda: bytearray(b"abc").replace(Raiser(), b"z"))
    _check("bytearray_removeprefix",
           lambda: bytearray(b"abc").removeprefix(Raiser()))
    _check("bytearray_strip", lambda: bytearray(b"abc").strip(Raiser()))
    _check("bytes_strip", lambda: b"abc".strip(Raiser()))
    _check("bytearray_split", lambda: bytearray(b"abc").split(Raiser()))
    _check("bytes_translate", lambda: b"abc".translate(None, Raiser()))
    _check("bytearray_translate",
           lambda: bytearray(b"abc").translate(None, Raiser()))
    return 0


def _setslice(other):
    ba = bytearray(b"abcdef")
    ba[1:3] = other
    return ba


if __name__ == "__main__":
    sys.exit(main())

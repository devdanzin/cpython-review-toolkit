"""init-bypass-checker — reproducer for the bytearray __new__/__init__ bypass SEGV.

Regression introduced by gh-139871 (commit 732224e1139, "Add bytearray.take_bytes")
which moved bytearray's buffer to an `ob_bytes_object` PyBytes holder and established
the "ob_bytes_object is always non-NULL" invariant in `bytearray___init___impl`
(Objects/bytearrayobject.c:923) rather than in tp_new.

`PyByteArray_Type.tp_new` is `PyType_GenericNew` (Objects/bytearrayobject.c:2940),
so any construction that skips tp_init leaves `ob_bytes_object == NULL`. Any method
that reaches `bytearray_resize_lock_held` (Objects/bytearrayobject.c:280) then calls
`_PyBytes_Resize(&obj->ob_bytes_object, alloc)`, which does `PyBytes_Check(v)` ->
`Py_TYPE(NULL)` -> SIGSEGV (Objects/bytesobject.c:3349).

Observed on CPython main 3.16.0a0 (debug + ASan): SIGSEGV, exit code 139.
Released 3.12.13 and 3.14.4 both return bytearray(b'\\x01') cleanly -> regression.

Run a single case:  ./python init_bypass_bytearray.py <case>
Cases: new, subclass, extend, iadd, insert, setitem
"""

import sys

CASES = {}


def case(fn):
    CASES[fn.__name__] = fn
    return fn


@case
def new():
    """T.__new__(T) — the direct bypass. SIGSEGV."""
    b = bytearray.__new__(bytearray)
    b.append(1)
    return b


@case
def subclass():
    """A subclass whose __init__ forgets super().__init__() — the innocent route. SIGSEGV."""

    class B(bytearray):
        def __init__(self, *a, **k):
            pass

    b = B()
    b.append(1)
    return b


@case
def extend():
    b = bytearray.__new__(bytearray)
    b.extend(b"ab")
    return b


@case
def iadd():
    b = bytearray.__new__(bytearray)
    b += b"ab"
    return b


@case
def insert():
    b = bytearray.__new__(bytearray)
    b.insert(0, 1)
    return b


@case
def setitem():
    b = bytearray.__new__(bytearray)
    b[0:0] = b"x"
    return b


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "new"
    print(f"running {name} ...", flush=True)
    print("result:", CASES[name]())
    print("SURVIVED (no crash) — the bug may be fixed in this build")

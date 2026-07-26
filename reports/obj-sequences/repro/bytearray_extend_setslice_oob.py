"""gh-153578 (PR #153579 still OPEN at the review ref and today): bytearray.extend()
evaluates lo = hi = Py_SIZE(self) BEFORE the argument's __buffer__ runs, and
bytearray_setslice only re-clamps `hi`.

Objects/bytearrayobject.c:2186   bytearray_setslice(self, Py_SIZE(self), Py_SIZE(self), it)
Objects/bytearrayobject.c:663    PyObject_GetBuffer(values, &vbytes, ...)  <- user __buffer__
Objects/bytearrayobject.c:675    if (hi < lo) hi = lo;          <- ordered before the size clamp
Objects/bytearrayobject.c:677    if (hi > Py_SIZE(self)) hi = Py_SIZE(self);   <- only hi
                                 => lo keeps its pre-callback value, lo > hi
Objects/bytearrayobject.c:561    assert(avail >= 0)  in bytearray_setslice_linear

Guarded twin: bytearray_iconcat_lock_held:356-362 acquires the buffer FIRST and
reads Py_SIZE(self) afterwards.

Confirm-don't-relitigate: this is a known open upstream issue. Run to establish
it is live at 4f3be1b5777.
"""

import sys


def main():
    b = bytearray(b"A" * 4000)

    class Evil:
        def __buffer__(self, flags):
            # Shrink self while extend() already captured lo = 4000.
            del b[:]
            return memoryview(b"BBBB")

    b.extend(Evil())
    print("no crash; len(b) =", len(b), "b[:16] =", bytes(b[:16]))


if __name__ == "__main__":
    main()
    print("completed")

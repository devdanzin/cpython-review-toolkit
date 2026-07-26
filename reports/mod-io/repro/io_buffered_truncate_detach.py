"""EP-2: _io._Buffered.truncate() dereferences self->raw AFTER driving a user write().

Modules/_io/bufferedio.c:1479-1485

    res = buffered_flush_and_rewind_unlocked(self);   /* -> raw.write() : user code */
    if (res == NULL) { goto end; }
    Py_CLEAR(res);
    res = PyObject_CallMethodOneArg(self->raw, &_Py_ID(truncate), pos);  /* NO re-check */

Note truncate() holds ENTER_BUFFERED, but detach() does NOT take that lock, so a
re-entrant detach() from inside raw.write() succeeds (the buffered flush that
detach itself drives is overridden to a no-op by the subclass).

Run with: <python> io_buffered_truncate_detach.py [--pyio]
"""

import sys

if "--pyio" in sys.argv:
    import _pyio as io
    backend = "_pyio"
else:
    import io
    backend = "_io (C)"


class Raw(io.RawIOBase):
    target = None

    def writable(self):
        return True

    def seekable(self):
        return True

    def write(self, b):
        if self.target is not None:
            t, self.target = self.target, None
            print("  re-entrant detach from raw.write ->",
                  type(t.detach()).__name__, file=sys.stderr)
        return len(b)

    def truncate(self, pos=None):
        return 0

    def seek(self, pos, whence=0):
        return 0

    def tell(self):
        return 0


class W(io.BufferedWriter):
    def flush(self):
        # no-op: lets the detach() driven from inside raw.write() get through
        return None


print("backend:", backend, file=sys.stderr)
raw = Raw()
w = W(raw, buffer_size=64)
w.write(b"0123456789abcdef")   # sits in the buffer; truncate() will flush it
raw.target = w                 # arm the trap only now
print("calling truncate()", file=sys.stderr)
try:
    w.truncate(0)
    print("RESULT: truncate() returned normally", file=sys.stderr)
except BaseException as exc:
    print("RESULT: truncate() raised %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
print("survived", file=sys.stderr)

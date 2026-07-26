"""EP-1: _io._Buffered.close() dereferences self->raw AFTER driving a user flush().

Modules/_io/bufferedio.c:582-591

    LEAVE_BUFFERED(self)
    r = _PyFile_Flush((PyObject *)self);      /* runs arbitrary Python */
    if (!ENTER_BUFFERED(self)) { return NULL; }
    ...
    res = PyObject_CallMethodNoArgs(self->raw, &_Py_ID(close));   /* NO re-check */

A re-entrant detach() from inside the user flush() sets self->raw = NULL, so the
line above calls PyObject_CallMethodNoArgs(NULL, ...) -> Py_TYPE(NULL) -> SIGSEGV.

Guarded twin: Modules/_io/textio.c uses buffer_access_safe() at every post-call
read of self->buffer (gh-143008 / commit db4b1948bc4). bufferedio.c was not
touched by that fix.

Run with: <python> io_buffered_close_detach.py [--pyio]
"""

import sys

if "--pyio" in sys.argv:
    import _pyio as io
    backend = "_pyio"
else:
    import io
    backend = "_io (C)"


class W(io.BufferedWriter):
    armed = True

    def flush(self):
        if self.armed:
            self.armed = False
            raw = super().detach()
            print("  re-entrant detach ->", type(raw).__name__, file=sys.stderr)
        return None


print("backend:", backend, file=sys.stderr)
w = W(io.BytesIO())
print("calling close()", file=sys.stderr)
try:
    w.close()
    print("RESULT: close() returned normally", file=sys.stderr)
except BaseException as exc:
    print("RESULT: close() raised %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
print("survived", file=sys.stderr)

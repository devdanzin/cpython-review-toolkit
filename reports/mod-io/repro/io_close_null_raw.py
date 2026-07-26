"""bufferedio.c:591 -- _io._Buffered.close() dispatches close() on a NULL self->raw.

_io__Buffered_close_impl (Modules/_io/bufferedio.c:551-604) runs

    LEAVE_BUFFERED(self)
    r = _PyFile_Flush((PyObject *)self);   /* :582 -- runs ARBITRARY user Python */
    if (!ENTER_BUFFERED(self)) { return NULL; }
    ...
    res = PyObject_CallMethodNoArgs(self->raw, &_Py_ID(close));   /* :591 */

`self->raw` was validated by CHECK_INITIALIZED at :556, *before* the flush.  A
user flush() that calls detach() sets self->raw = NULL and self->ok = 0, and the
call at :591 is made on a NULL receiver with no re-check.

Guarded twin: Modules/_io/textio.c:1634-1641 does the identical flush and then
re-validates with buffer_access_safe(), with a comment naming the exact hazard:
    /* _PyFile_Flush could detach before returning; raise an exception. */

_pyio twin: Lib/_pyio.py:1345 `if self.raw is not None and not self.closed:` --
guarded; the AttributeError it raises comes from the *second*, unguarded
`self.raw.close()` at :1347, which is a (separate, much milder) _pyio bug.
"""

import io
import sys


class Evil(io.BufferedWriter):
    armed = True

    def flush(self):
        if self.armed:
            self.armed = False
            # detach() NULLs self->raw while close() is mid-flight
            super().detach()
            print("inner detach done", file=sys.stderr)


e = Evil(io.BytesIO())
print("calling close", file=sys.stderr)
try:
    e.close()
    print("close returned", file=sys.stderr)
except BaseException as exc:
    print("close raised %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
print("survived", file=sys.stderr)

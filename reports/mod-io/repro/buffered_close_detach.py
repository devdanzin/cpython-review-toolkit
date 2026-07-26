"""_io._Buffered.close: `self->raw` is re-read AFTER the lock is dropped for a
user flush, with no re-check.

Modules/_io/bufferedio.c:580  LEAVE_BUFFERED(self)            <- lock dropped on purpose
Modules/_io/bufferedio.c:581  r = _PyFile_Flush((PyObject *)self)   <- arbitrary user Python
Modules/_io/bufferedio.c:591  res = PyObject_CallMethodNoArgs(self->raw, &_Py_ID(close))

A re-entrant `detach()` inside the flush sets `self->raw = NULL`
(bufferedio.c:626) -- close() then calls a method on NULL.

The guarded twin is `textio.c:1638`, which re-derives the handle through
`buffer_access_safe()` after its own `_PyFile_Flush`.

Usage:  python buffered_close_detach.py [--pyio]
"""

import sys

if "--pyio" in sys.argv:
    import _pyio as io
else:
    import io


fired = False


class Reader(io.BufferedReader):
    def flush(self):
        global fired
        if not fired:
            fired = True
            try:
                self.detach()
            except Exception as exc:  # noqa: BLE001
                print("  inner detach raised", type(exc).__name__, exc, flush=True)
        return None


b = Reader(io.BytesIO(b"payload" * 100))
print("start", flush=True)
try:
    b.close()
except Exception as exc:  # noqa: BLE001
    print("close raised", type(exc).__name__, exc, flush=True)
else:
    print("close returned", flush=True)
print("survived", flush=True)

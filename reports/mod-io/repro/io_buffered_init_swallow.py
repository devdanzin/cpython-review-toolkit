"""EP-S1: _buffered_init()'s unnarrowed PyErr_Clear() swallows whatever the raw
stream's tell() raised.

Modules/_io/bufferedio.c:869-870
    if (_buffered_raw_tell(self) == -1)
        PyErr_Clear();

_buffered_raw_tell -> PyObject_CallMethodNoArgs(self->raw, &_Py_ID(tell))
(bufferedio.c:788), i.e. arbitrary user Python.  The clear is not narrowed by
PyErr_ExceptionMatches, so a KeyboardInterrupt / MemoryError / RecursionError
raised by a user tell() is discarded and the constructor reports success.

Sibling site with the identical shape: bufferedio.c:1489-1490 in
_io._Buffered.truncate (only the :870 one was reported by the scanner).

Guarded twins in the same file:
  :881  _PyIO_trap_eintr           narrows with PyErr_ExceptionMatches(OSError)
  :1547 buffered_repr             narrows with PyErr_ExceptionMatches(ValueError)

usage: python io_buffered_init_swallow.py [--pyio]
"""

import sys

if "--pyio" in sys.argv:
    import _pyio as io
    backend = "_pyio"
else:
    import io
    backend = "_io (C)"


class Raw(io.RawIOBase):
    kind = KeyboardInterrupt

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        raise self.kind("tell() says stop")

    def readinto(self, b):
        return 0


print("backend:", backend, file=sys.stderr)
for exc in (KeyboardInterrupt, MemoryError, RecursionError, SystemExit):
    Raw.kind = exc
    try:
        b = io.BufferedReader(Raw())
        print("  %-18s -> constructor SUCCEEDED, exception swallowed"
              % exc.__name__, file=sys.stderr)
    except BaseException as e:
        print("  %-18s -> propagated as %s" % (exc.__name__, type(e).__name__),
              file=sys.stderr)

# same shape, second site: truncate()  (bufferedio.c:1489-1490)
class WRaw(Raw):
    def writable(self):
        return True

    def write(self, b):
        return len(b)

    def truncate(self, pos=None):
        return 0

    def seek(self, pos, whence=0):
        return 0

    _tell_armed = False

    def tell(self):
        if self._tell_armed:
            raise KeyboardInterrupt("tell() says stop")
        return 0


raw = WRaw()
w = io.BufferedWriter(raw)
raw._tell_armed = True
try:
    w.truncate(0)
    print("  truncate()         -> SUCCEEDED, KeyboardInterrupt swallowed"
          " (bufferedio.c:1490)", file=sys.stderr)
except BaseException as e:
    print("  truncate()         -> propagated as %s" % type(e).__name__,
          file=sys.stderr)
print("survived", file=sys.stderr)

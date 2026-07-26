"""EP-S2: _io.FileIO.__init__ swallows whatever the name argument's __index__
raised, then mis-reports the failure as a TypeError about the filename.

Modules/_io/fileio.c:296-304

    fd = PyLong_AsInt(nameobj);
    if (fd < 0) {
        if (!PyErr_Occurred()) {
            PyErr_SetString(PyExc_ValueError, "negative file descriptor");
            return -1;
        }
        PyErr_Clear();            <-- unnarrowed
    }

`nameobj` is an arbitrary object, so PyLong_AsInt dispatches its __index__.
The clear is meant to say "not an fd, try it as a filename", but it discards
KeyboardInterrupt / MemoryError / RecursionError as readily as the TypeError
the author had in mind.

Guarded twin in the same file family: Modules/_io/_iomodule.c:541 narrows with
PyErr_GivenExceptionMatches(runerr, PyExc_OverflowError) before clearing.

usage: python io_fileio_index_swallow.py [--pyio]
"""

import sys

if "--pyio" in sys.argv:
    import _pyio as io
    backend = "_pyio"
else:
    import io
    backend = "_io (C)"


class EvilIndex:
    kind = KeyboardInterrupt

    def __index__(self):
        raise self.kind("__index__ says stop")


print("backend:", backend, file=sys.stderr)
for exc in (KeyboardInterrupt, MemoryError, RecursionError, SystemExit,
            TypeError):
    EvilIndex.kind = exc
    try:
        io.FileIO(EvilIndex())
        print("  %-18s -> constructor SUCCEEDED (!)" % exc.__name__,
              file=sys.stderr)
    except BaseException as e:
        verdict = "PROPAGATED" if isinstance(e, exc) else "SWALLOWED, reported as"
        print("  %-18s -> %s %s: %s"
              % (exc.__name__, verdict, type(e).__name__, str(e)[:60]),
              file=sys.stderr)
print("survived", file=sys.stderr)

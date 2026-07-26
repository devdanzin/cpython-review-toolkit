"""Stale-self->raw sites in bufferedio.c that scan_init_bypass did NOT flag.

The scanner's sink set is {Py_INCREF, Py_NewRef, PyObject_Call*, deref macros,
_PyBytes_Resize, one-hop param deref}.  These three sites pass a NULL
self->raw to something outside that set, and each one dies just as hard:

  readall_getattr  bufferedio.c:1713  PyObject_GetOptionalAttr(self->raw, ...)
  seek_seekable    bufferedio.c:1389  _PyIOBase_check_seekable(state, self->raw, ...)
  dealloc_warn     bufferedio.c:489   -- CONTROL: this one IS guarded
                                         (`if (self->ok && self->raw)`), the
                                         guarded twin inside the same file.
"""

import io
import os
import sys

if os.environ.get("PYIO"):
    import _pyio as io


def sc_readall_getattr():
    """bufferedio.c:1713 -- _bufferedreader_read_all reaches
    PyObject_GetOptionalAttr(self->raw, readall) after
    buffered_flush_and_rewind_unlocked ran raw.seek()."""

    class EvilRaw:
        closed = False
        owner = None

        def readable(self):
            return True

        def writable(self):
            return True

        def seekable(self):
            return True

        def read(self, n=-1):
            return b""

        def readinto(self, b):
            return 0

        def write(self, b):
            return len(b)

        def flush(self):
            return None

        def tell(self):
            return 0

        def seek(self, p, w=0):
            # detach from inside raw.seek(): _buffered_raw_seek has already
            # made its call, so it returns cleanly and control continues to
            # the unguarded GetOptionalAttr.
            o = self.owner
            self.owner = None
            if o is not None:
                try:
                    io.BufferedRandom.detach(o)
                    sys.stderr.write("  inner detach OK\n")
                except BaseException as e:
                    sys.stderr.write("  inner detach failed: %r\n" % (e,))
            return 0

    class Evil(io.BufferedRandom):
        def flush(self):
            return None

    raw = EvilRaw()
    b = Evil(raw, buffer_size=64)
    b.write(b"z" * 40)          # make the flush-and-rewind path do real work
    raw.owner = b
    return b.read()


def sc_seek_seekable():
    """bufferedio.c:1389 -- _io__Buffered_seek_impl passes self->raw to
    _PyIOBase_check_seekable() right after CHECK_CLOSED ran the raw object's
    `closed` property, which detached."""

    class EvilRaw:
        owner = None

        @property
        def closed(self):
            o = self.owner
            self.owner = None
            if o is not None:
                try:
                    io.BufferedWriter.detach(o)
                    sys.stderr.write("  inner detach OK\n")
                except BaseException as e:
                    sys.stderr.write("  inner detach failed: %r\n" % (e,))
            return False

        def writable(self):
            return True

        def readable(self):
            return False

        def seekable(self):
            return True

        def write(self, b):
            return len(b)

        def seek(self, p, w=0):
            return p

        def tell(self):
            return 0

        def flush(self):
            return None

    class Evil(io.BufferedWriter):
        def flush(self):
            return None

    raw = EvilRaw()
    b = Evil(raw, buffer_size=64)
    raw.owner = b
    return b.seek(0)


def sc_dealloc_warn():
    """CONTROL -- bufferedio.c:489 `if (self->ok && self->raw)` is the
    guarded twin.  Detaching then triggering the dealloc warning must NOT
    crash."""

    class R:
        closed = False

        def writable(self):
            return True

        def write(self, b):
            return len(b)

        def flush(self):
            return None

        def close(self):
            self.closed = True

    class Evil(io.BufferedWriter):
        def flush(self):
            return None

    b = Evil(R(), buffer_size=64)
    io.BufferedWriter.detach(b)
    del b
    return "no crash"


SCENARIOS = {
    name[3:]: fn for name, fn in sorted(globals().items()) if name.startswith("sc_")
}

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--list"
    if arg == "--list":
        for k, fn in SCENARIOS.items():
            print(k, "--", (fn.__doc__ or "").split("\n")[0])
        sys.exit(0)
    sys.stderr.write("RUN %s\n" % arg)
    sys.stderr.flush()
    try:
        out = SCENARIOS[arg]()
        sys.stderr.write("SURVIVED -> %.80r\n" % (out,))
    except BaseException as exc:
        sys.stderr.write("RAISED %s: %s\n" % (type(exc).__name__, exc))

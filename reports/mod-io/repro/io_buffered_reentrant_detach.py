"""Sibling hunt for the seeded lead (AGENT_BRIEF section 3).

bufferedio.c reads self->raw AFTER a call that can run arbitrary user Python
and NULLs the field.  The re-entrancy point is either
  (a) _PyFile_Flush((PyObject *)self)   -- dispatches to a Python flush() override
  (b) a method call THROUGH self->raw   -- dispatches to a user raw object

Each scenario names the bufferedio.c line whose `self->raw` read is stale.

Usage:  python io_buffered_reentrant_detach.py <scenario>
        python io_buffered_reentrant_detach.py --list
"""

import io
import os
import sys

if os.environ.get("PYIO"):
    import _pyio as io


def make_evil_raw(hook, base=None):
    """A raw object whose write()/read()/readinto()/truncate() calls `hook`."""
    src = io.BytesIO(base if base is not None else b"abcdefghij" * 2000)

    class EvilRaw(io.RawIOBase):
        def __init__(self):
            self.armed = True
            self.owner = None

        def _fire(self):
            if self.armed and self.owner is not None:
                self.armed = False
                hook(self.owner)

        def readable(self):
            return True

        def writable(self):
            return True

        def seekable(self):
            return True

        def readinto(self, b):
            self._fire()
            data = src.read(len(b))
            b[: len(data)] = data
            return len(data)

        def read(self, n=-1):
            self._fire()
            return src.read(n)

        def readall(self):
            self._fire()
            return src.read()

        def write(self, b):
            self._fire()
            return len(b)

        def seek(self, pos, whence=0):
            return src.seek(pos, whence)

        def tell(self):
            return src.tell()

        def truncate(self, pos=None):
            self._fire()
            return 0

    return EvilRaw()


def detach_hook(buf):
    for T in (io.BufferedRandom, io.BufferedWriter, io.BufferedReader):
        if isinstance(buf, T):
            try:
                T.detach(buf)
                sys.stderr.write("  inner detach OK via %s\n" % T.__name__)
            except Exception as e:
                sys.stderr.write("  inner detach failed: %r\n" % (e,))
            return
    sys.stderr.write("  inner detach: no matching type for %r\n" % (buf,))


# ---------------------------------------------------------------- scenarios


def sc_detach():
    """bufferedio.c:623 -- the seeded lead. detach() re-reads self->raw
    after _PyFile_Flush, returns NULL with no exception set."""

    class Evil(io.BufferedReader):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.armed = True

        def flush(self):
            if self.armed:
                self.armed = False
                super().detach()
            return None

    e = Evil(io.BytesIO(b"abc"))
    return e.detach()


def sc_close():
    """bufferedio.c:591 -- close() calls PyObject_CallMethodNoArgs(self->raw,
    close) after LEAVE_BUFFERED + _PyFile_Flush ran a Python flush()."""

    class Evil(io.BufferedWriter):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.armed = True

        def flush(self):
            if self.armed:
                self.armed = False
                io.BufferedWriter.detach(self)
            return None

    e = Evil(io.BytesIO())
    return e.close()


def sc_truncate():
    """bufferedio.c:1485 -- truncate() calls PyObject_CallMethodOneArg(
    self->raw, truncate, pos) after buffered_flush_and_rewind_unlocked drove
    a user raw.write()."""
    raw = make_evil_raw(detach_hook)
    b = io.BufferedWriter(raw, buffer_size=64)
    raw.owner = b
    b.write(b"x" * 40)          # buffered, not yet flushed
    return b.truncate(0)


def sc_writer_loop():
    """bufferedio.c:1996 -- _bufferedwriter_raw_write is called in a loop
    from _bufferedwriter_flush_unlocked; iteration N+1 re-reads self->raw
    after iteration N ran user Python."""
    raw = make_evil_raw(detach_hook)
    b = io.BufferedWriter(raw, buffer_size=64)
    raw.owner = b
    # force a multi-iteration flush: partial writes -> loop keeps going
    return b.write(b"y" * 4096)


def sc_read_all():
    """bufferedio.c:1748 -- _bufferedreader_read_all loops on
    PyObject_CallMethodNoArgs(self->raw, read)."""
    raw = make_evil_raw(detach_hook)
    b = io.BufferedReader(raw, buffer_size=64)
    raw.owner = b
    return b.read()


def sc_read_all_getattr():
    """bufferedio.c:1713 -- PyObject_GetOptionalAttr(self->raw, readall)
    right after buffered_flush_and_rewind_unlocked ran user write().
    (Not a scanner sink -- recall gap.)"""
    raw = make_evil_raw(detach_hook)
    b = io.BufferedRandom(raw, buffer_size=64)
    raw.owner = b
    b.write(b"z" * 40)
    return b.read()


def sc_reader_fill():
    """bufferedio.c:1640 -- _bufferedreader_raw_read is called in a loop from
    _bufferedreader_read_generic; the re-entrancy comes from raw.readinto()."""
    raw = make_evil_raw(detach_hook)
    b = io.BufferedReader(raw, buffer_size=64)
    raw.owner = b
    return b.read(4096)


def sc_raw_tell():
    """bufferedio.c:788 -- _buffered_raw_tell reads self->raw; reached from
    tell() after seek/flush machinery ran user code."""
    raw = make_evil_raw(detach_hook)
    b = io.BufferedRandom(raw, buffer_size=64)
    raw.owner = b
    b.write(b"q" * 40)
    return b.tell()


def sc_raw_seek():
    """bufferedio.c:818 -- _buffered_raw_seek reads self->raw after
    _bufferedwriter_flush_unlocked ran user write()."""
    raw = make_evil_raw(detach_hook)
    b = io.BufferedRandom(raw, buffer_size=64)
    raw.owner = b
    b.write(b"w" * 40)
    return b.seek(0)


def sc_closed_getattr():
    """bufferedio.c:526 buffered_closed / :544 closed getter --
    PyObject_GetAttr(self->raw, closed).  Re-entrancy via a `closed`
    property on the raw object."""

    holder = {}

    class EvilRaw(io.RawIOBase):
        @property
        def closed(self):
            b = holder.get("b")
            if b is not None and holder.pop("armed", False):
                try:
                    io.BufferedWriter.detach(b)
                except Exception as e:
                    sys.stderr.write("  inner detach failed: %r\n" % (e,))
            return False

        def writable(self):
            return True

        def write(self, b):
            return len(b)

    raw = EvilRaw()
    b = io.BufferedWriter(raw, buffer_size=64)
    holder["b"] = b
    holder["armed"] = True
    return b.truncate(0)


SCENARIOS = {
    name[3:]: fn for name, fn in sorted(globals().items()) if name.startswith("sc_")
}

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--list"
    if arg == "--list":
        for k, fn in SCENARIOS.items():
            print(k, "--", (fn.__doc__ or "").split("\n")[0])
        sys.exit(0)
    fn = SCENARIOS[arg]
    sys.stderr.write("RUN %s\n" % arg)
    sys.stderr.flush()
    try:
        out = fn()
        sys.stderr.write("SURVIVED -> %r\n" % (out,))
    except BaseException as exc:
        sys.stderr.write("RAISED %s: %s\n" % (type(exc).__name__, exc))

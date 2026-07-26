"""Round 2 of the bufferedio.c stale-self->raw sibling hunt.

Round 1 (io_buffered_reentrant_detach.py) showed that a re-entrant detach()
is *accidentally* blocked whenever the outer frame holds ENTER_BUFFERED,
because detach() -> _PyFile_Flush() -> self.flush() -> _io__Buffered_flush_impl
-> ENTER_BUFFERED -> _enter_buffered_busy -> RuntimeError("reentrant call").

That protection evaporates the moment the object is a *Python subclass that
overrides flush()* -- exactly the shape the seeded lead used.  Round 2 puts
the flush() override on every scenario, so the inner detach succeeds even
inside an ENTER_BUFFERED region.

Each scenario names the bufferedio.c line whose self->raw read is stale.
"""

import io
import os
import sys

if os.environ.get("PYIO"):
    import _pyio as io


def make_buffered(base, raw, **kw):
    """A `base` subclass whose flush() is a Python no-op, so a re-entrant
    detach() is not blocked by _enter_buffered_busy."""

    class Evil(base):
        armed_detach = False

        def flush(self):
            return None

        def fire(self):
            if self.armed_detach:
                self.armed_detach = False
                try:
                    base.detach(self)
                    sys.stderr.write("  inner detach OK\n")
                except BaseException as e:
                    sys.stderr.write("  inner detach failed: %r\n" % (e,))

    b = Evil(raw, **kw)
    return b


def make_raw(hook_holder, src=b"abcdefghij" * 4000, plain=False, partial=0):
    """A raw stream that calls hook_holder['b'].fire() from its I/O methods.
    plain=True -> not a RawIOBase subclass, so it has no readall()."""
    state = {"pos": 0}

    class Body:
        closed = False

        def readable(self):
            return True

        def writable(self):
            return True

        def seekable(self):
            return True

        def _fire(self):
            b = hook_holder.get("b")
            if b is not None:
                b.fire()

        def readinto(self, b):
            self._fire()
            n = min(len(b), partial) if partial else len(b)
            data = src[state["pos"]:state["pos"] + n]
            state["pos"] += len(data)
            b[: len(data)] = data
            return len(data)

        def read(self, n=-1):
            self._fire()
            if n is None or n < 0:
                n = len(src) - state["pos"]
            data = src[state["pos"]:state["pos"] + n]
            state["pos"] += len(data)
            return data

        def write(self, b):
            self._fire()
            return min(len(b), partial) if partial else len(b)

        def seek(self, pos, whence=0):
            self._fire()
            state["pos"] = pos
            return pos

        def tell(self):
            return state["pos"]

        def truncate(self, pos=None):
            self._fire()
            return 0

        def flush(self):
            return None

        def close(self):
            self.closed = True

    if plain:
        return Body()

    class RawImpl(Body, io.RawIOBase):
        pass

    return RawImpl()


def arm(b):
    b.armed_detach = True
    return b


# ---------------------------------------------------------------- scenarios


def sc_truncate():
    """bufferedio.c:1485 -- PyObject_CallMethodOneArg(self->raw, truncate)
    after buffered_flush_and_rewind_unlocked ran raw.write()."""
    h = {}
    raw = make_raw(h)
    b = make_buffered(io.BufferedWriter, raw, buffer_size=64)
    h["b"] = b
    b.write(b"x" * 40)
    arm(b)
    return b.truncate(0)


def sc_writer_loop():
    """bufferedio.c:1996 -- _bufferedwriter_raw_write re-reads self->raw on
    the next iteration of the flush loop."""
    h = {}
    raw = make_raw(h, partial=16)   # partial writes -> the flush loop iterates
    b = make_buffered(io.BufferedWriter, raw, buffer_size=64)
    h["b"] = b
    arm(b)
    return b.write(b"y" * 8192)


def sc_read_all():
    """bufferedio.c:1748 -- _bufferedreader_read_all's `data =
    PyObject_CallMethodNoArgs(self->raw, read)` loop.  Uses a raw with no
    readall(), so the loop (not the readall fast path) is taken."""
    h = {}
    raw = make_raw(h, plain=True)
    b = make_buffered(io.BufferedReader, raw, buffer_size=64)
    h["b"] = b
    arm(b)
    return b.read()


def sc_read_all_getattr():
    """bufferedio.c:1713 -- PyObject_GetOptionalAttr(self->raw, readall)
    after buffered_flush_and_rewind_unlocked ran raw.write().
    NOT a scanner sink (GetOptionalAttr is not in the sink set)."""
    h = {}
    raw = make_raw(h)
    b = make_buffered(io.BufferedRandom, raw, buffer_size=64)
    h["b"] = b
    b.write(b"z" * 40)
    arm(b)
    return b.read()


def sc_reader_fill():
    """bufferedio.c:1640 -- _bufferedreader_raw_read re-reads self->raw on
    the next iteration of _bufferedreader_read_generic's fill loop."""
    h = {}
    raw = make_raw(h, partial=16)   # partial reads -> the fill loop iterates
    b = make_buffered(io.BufferedReader, raw, buffer_size=64)
    h["b"] = b
    arm(b)
    return b.read(8192)


def sc_raw_seek():
    """bufferedio.c:818 -- _buffered_raw_seek reads self->raw after
    _bufferedwriter_flush_unlocked ran raw.write()."""
    h = {}
    raw = make_raw(h)
    b = make_buffered(io.BufferedRandom, raw, buffer_size=64)
    h["b"] = b
    b.write(b"w" * 40)
    arm(b)
    return b.seek(0)


def sc_raw_tell():
    """bufferedio.c:788 -- _buffered_raw_tell reads self->raw."""
    h = {}
    raw = make_raw(h)
    b = make_buffered(io.BufferedRandom, raw, buffer_size=64)
    h["b"] = b
    b.write(b"q" * 4096)
    arm(b)
    return b.tell()


def sc_simple_flush():
    """bufferedio.c:517 -- _io__Buffered_simple_flush_impl reads self->raw
    IMMEDIATELY after CHECK_INITIALIZED, with nothing in between.
    Control: this one must NOT be reachable."""
    h = {}
    raw = make_raw(h)
    b = io.BufferedWriter(raw, buffer_size=64)
    h["b"] = b
    return io.BufferedWriter.flush(b)


def sc_seekable():
    """bufferedio.c:644/657/670/714/727 -- the five one-liner inquiry
    methods.  Control: nothing runs between CHECK_INITIALIZED and the read."""
    h = {}
    raw = make_raw(h)
    b = io.BufferedWriter(raw, buffer_size=64)
    h["b"] = b
    return (b.seekable(), b.readable(), b.writable(), b.isatty())


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

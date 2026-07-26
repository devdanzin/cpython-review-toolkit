"""Sweep: every `self->raw` read in Modules/_io/bufferedio.c that happens AFTER a
call into arbitrary user Python, with no re-check.

This is the bufferedio.c sibling family of gh-143008, which fixed exactly this
shape in Modules/_io/textio.c by routing every post-call `self->buffer` read
through buffer_access_safe().  bufferedio.c was not touched by that commit and
has no equivalent accessor.

Each case arranges for a re-entrant `detach()` to run from inside a user callback
that a buffered method drives, then lets the method continue and use `self->raw`.

usage:  python io_buffered_stale_raw_sweep.py --case <name> [--pyio]
        python io_buffered_stale_raw_sweep.py --list
"""

import sys

PYIO = "--pyio" in sys.argv
if PYIO:
    import _pyio as io
else:
    import io


def arm(obj, victim):
    """Return a one-shot callback that detaches `victim`."""
    state = {"fired": False}

    def fire():
        if not state["fired"]:
            state["fired"] = True
            try:
                r = victim.detach()
                print("  [detached ->", type(r).__name__, "]", file=sys.stderr)
            except BaseException as e:
                print("  [detach raised %s: %s]" % (type(e).__name__, e),
                      file=sys.stderr)
    return fire


# ---------------------------------------------------------------- raw helpers

class BaseRaw(io.RawIOBase):
    """A RawIOBase whose every hook can be armed with a one-shot callback."""

    def __init__(self, data=b""):
        self._data = bytearray(data)
        self._pos = 0
        self.on_read = None
        self.on_write = None
        self.on_closed = None
        self.on_seek = None
        self.short_write = False

    def readable(self):
        return True

    def writable(self):
        return True

    def seekable(self):
        return True

    def _fire(self, name):
        cb = getattr(self, name)
        if cb is not None:
            setattr(self, name, None)
            cb()

    @property
    def closed(self):
        self._fire("on_closed")
        return False

    def readinto(self, b):
        self._fire("on_read")
        n = min(len(b), len(self._data) - self._pos)
        b[:n] = self._data[self._pos:self._pos + n]
        self._pos += n
        return n

    def read(self, n=-1):
        self._fire("on_read")
        if n is None or n < 0:
            n = len(self._data) - self._pos
        out = bytes(self._data[self._pos:self._pos + n])
        self._pos += len(out)
        return out

    def write(self, b):
        self._fire("on_write")
        n = 1 if (self.short_write and len(b) > 1) else len(b)
        self._data[self._pos:self._pos + n] = bytes(b)[:n]
        self._pos += n
        return n

    def seek(self, pos, whence=0):
        self._fire("on_seek")
        if whence == 0:
            self._pos = pos
        elif whence == 1:
            self._pos += pos
        else:
            self._pos = len(self._data) + pos
        return self._pos

    def tell(self):
        return self._pos

    def truncate(self, pos=None):
        return self._pos if pos is None else pos


class QuietFlushWriter(io.BufferedWriter):
    """flush() overridden to a no-op so a re-entrant detach() can complete."""
    def flush(self):
        return None


class QuietFlushReader(io.BufferedReader):
    def flush(self):
        return None


class QuietFlushRandom(io.BufferedRandom):
    def flush(self):
        return None


# ---------------------------------------------------------------------- cases

def case_close_via_flush():
    """bufferedio.c:591 -- close() reads self->raw after _PyFile_Flush()."""
    class W(io.BufferedWriter):
        armed = True

        def flush(self):
            if self.armed:
                self.armed = False
                super().detach()
            return None
    w = W(io.BytesIO())
    return w.close()


def case_truncate_via_write():
    """bufferedio.c:1485 -- truncate() reads self->raw after flush_and_rewind."""
    raw = BaseRaw()
    w = QuietFlushWriter(raw, buffer_size=64)
    w.write(b"0123456789abcdef")
    raw.on_write = arm(raw, w)
    return w.truncate(0)


def case_readall_via_read():
    """bufferedio.c:1748 -- _bufferedreader_read_all loops on self->raw.read()."""
    raw = BaseRaw(b"x" * 32)
    # hide .readall so the generic read() loop is taken
    del raw.__class__.readall
    r = QuietFlushReader(raw, buffer_size=8)
    raw.on_read = arm(raw, r)
    return r.read()


def case_readall_via_closedprop():
    """bufferedio.c:1713 -- read_all reads self->raw after CHECK_CLOSED ran
    the user `closed` property."""
    raw = BaseRaw(b"x" * 32)
    r = QuietFlushReader(raw, buffer_size=8)
    raw.on_closed = arm(raw, r)
    return r.read()


def case_seek_via_closedprop():
    """bufferedio.c:1389 -- seek() passes self->raw to _PyIOBase_check_seekable
    after CHECK_CLOSED ran the user `closed` property."""
    raw = BaseRaw(b"x" * 32)
    r = QuietFlushRandom(raw, buffer_size=8)
    raw.on_closed = arm(raw, r)
    return r.seek(0)


def case_flushloop_via_write():
    """bufferedio.c:1996 -- _bufferedwriter_flush_unlocked loops, re-reading
    self->raw for each partial write."""
    raw = BaseRaw()
    raw.short_write = True          # 1 byte per write -> many loop iterations
    w = QuietFlushWriter(raw, buffer_size=8)
    w.write(b"abcdefgh")            # fills the buffer
    raw.on_write = arm(raw, w)
    return w.write(b"ijklmnop")     # forces the flush loop


def case_rewind_via_write():
    """bufferedio.c:818 -- buffered_flush_and_rewind_unlocked calls
    _buffered_raw_seek(self->raw) after _bufferedwriter_flush_unlocked ran
    user raw.write()."""
    raw = BaseRaw(b"y" * 64)
    b = QuietFlushRandom(raw, buffer_size=16)
    b.write(b"abcd")
    raw.on_write = arm(raw, b)
    return b.read(4)                # read path flushes+rewinds first


def case_write_via_closedprop():
    """bufferedio.c:2118 -- BufferedWriter.write memcpy's into self->buffer
    after IS_CLOSED ran the user `closed` property."""
    raw = BaseRaw()
    w = QuietFlushWriter(raw, buffer_size=64)
    raw.on_closed = arm(raw, w)
    return w.write(b"abcd")


def case_detach_via_flush():
    """bufferedio.c:625 -- the seeded lead; detach returns NULL, no exception."""
    class R(io.BufferedReader):
        armed = True

        def flush(self):
            if self.armed:
                self.armed = False
                super().detach()
            return None
    r = R(io.BytesIO(b"abc"))
    return r.detach()


def case_peek_via_closedprop():
    """bufferedio.c:977 -- peek() -> _bufferedreader_peek_unlocked after
    CHECK_CLOSED ran the user `closed` property."""
    raw = BaseRaw(b"x" * 32)
    r = QuietFlushReader(raw, buffer_size=8)
    raw.on_closed = arm(raw, r)
    return r.peek(4)


def case_readline_via_closedprop():
    """bufferedio.c:1265 -- readline() -> fill_buffer after CHECK_CLOSED ran
    the user `closed` property."""
    raw = BaseRaw(b"x" * 32)
    r = QuietFlushReader(raw, buffer_size=8)
    raw.on_closed = arm(raw, r)
    return r.readline()


def case_readinto_via_closedprop():
    """bufferedio.c:1141 -- readinto() -> fill_buffer after CHECK_CLOSED."""
    raw = BaseRaw(b"x" * 32)
    r = QuietFlushReader(raw, buffer_size=8)
    raw.on_closed = arm(raw, r)
    buf = bytearray(16)
    return r.readinto(buf)


def case_tell_via_closedprop():
    """bufferedio.c:1341 -- tell() -> _buffered_raw_tell(self->raw).
    tell() has no CHECK_CLOSED, so this one should be clean."""
    raw = BaseRaw(b"x" * 32)
    r = QuietFlushReader(raw, buffer_size=8)
    raw.on_closed = arm(raw, r)
    return r.tell()


# textio control cases -- the guarded twin; expected to raise cleanly
def case_textio_close_via_flush():
    class T(io.TextIOWrapper):
        armed = True

        def flush(self):
            if self.armed:
                self.armed = False
                super().detach()
            return None
    t = T(io.BytesIO(b"abc"))
    return t.close()


def case_textio_detach_via_flush():
    class T(io.TextIOWrapper):
        armed = True

        def flush(self):
            if self.armed:
                self.armed = False
                super().detach()
            return None
    t = T(io.BytesIO(b"abc"))
    return t.detach()


CASES = {k[5:]: v for k, v in sorted(globals().items()) if k.startswith("case_")}


def main():
    if "--list" in sys.argv:
        for name in CASES:
            print(name)
        return
    i = sys.argv.index("--case")
    name = sys.argv[i + 1]
    print("case=%s backend=%s" % (name, "_pyio" if PYIO else "_io"), file=sys.stderr)
    try:
        out = CASES[name]()
        print("RESULT: returned %r" % (out,), file=sys.stderr)
    except BaseException as exc:
        print("RESULT: raised %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
    print("survived", file=sys.stderr)


if __name__ == "__main__":
    main()

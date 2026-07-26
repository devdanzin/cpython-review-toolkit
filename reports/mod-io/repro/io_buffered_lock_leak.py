"""Dynamic counterpart to the hand-audit of the 12 ENTER_BUFFERED spans.

A leaked ENTER_BUFFERED does not crash.  It leaves `self->lock` held and
`self->owner` set to the leaking thread, so EVERY subsequent call from that same
thread hits the `owner == PyThread_get_thread_ident()` branch of
`_enter_buffered_busy` (bufferedio.c:299-303) and raises

    RuntimeError: reentrant call inside <_io.BufferedReader ...>

forever.  The object is bricked, not merely contended.  That gives a precise,
single-threaded oracle for the leak class:

    drive a method through an error path INSIDE its span,
    then call a method that takes the lock again --
    if it raises "reentrant call inside", the span leaked.

This is the shape of gh-143689 (BufferedReader.read1 leaked on the
PyBytesWriter_Create failure path, fixed by 375e372c666 which added the missing
LEAVE_BUFFERED).  Scenario `read1_huge` below is that issue's own reproducer.

Usage:  <python> io_buffered_lock_leak.py [--pyio]
Exit code 0 = no span leaked.  1 = at least one span leaked.
"""

import sys
import traceback

USE_PYIO = "--pyio" in sys.argv
if USE_PYIO:
    import _pyio as iomod
else:
    import io as iomod

BRICKED = "reentrant call inside"


class Boom(Exception):
    pass


class RaisingRaw(iomod.RawIOBase):
    """A raw stream whose every operation fails, from inside the buffered span."""

    def __init__(self, mode="rb", fail_on=()):
        self._fail_on = set(fail_on)
        self._mode = mode
        self._data = b"x" * 4096
        self._pos = 0

    def readable(self):
        return "r" in self._mode

    def writable(self):
        return "w" in self._mode

    def seekable(self):
        return True

    def _maybe(self, what):
        if what in self._fail_on:
            raise Boom(what)

    def readinto(self, b):
        self._maybe("read")
        n = min(len(b), len(self._data) - self._pos)
        b[:n] = self._data[self._pos:self._pos + n]
        self._pos += n
        return n

    def write(self, b):
        self._maybe("write")
        return len(b)

    def seek(self, pos, whence=0):
        self._maybe("seek")
        self._pos = pos
        return pos

    def tell(self):
        self._maybe("tell")
        return self._pos

    def truncate(self, pos=None):
        self._maybe("truncate")
        return pos or self._pos

    def flush(self):
        self._maybe("flush")


def reader(fail_on=(), **kw):
    return iomod.BufferedReader(RaisingRaw("rb", fail_on), **kw)


def writer(fail_on=(), **kw):
    return iomod.BufferedWriter(RaisingRaw("wb", fail_on), **kw)


def random_(fail_on=(), **kw):
    return iomod.BufferedRandom(RaisingRaw("rwb", fail_on), **kw)


# (name, factory, action) -- action drives an error path inside a span.
SCENARIOS = [
    # gh-143689's own reproducer: read1 with a huge size -> MemoryError while
    # allocating the output buffer, inside the :1059 span.
    ("read1_huge",      lambda: iomod.BufferedReader(iomod.BytesIO(b"hello"),
                                                     buffer_size=8),
     lambda f: f.read1(10 ** 18)),
    ("read_all_raises", lambda: reader(("read",)),           lambda f: f.read()),
    ("read_n_raises",   lambda: reader(("read",)),           lambda f: f.read(4096)),
    ("read1_raises",    lambda: reader(("read",)),           lambda f: f.read1(4096)),
    ("peek_raises",     lambda: reader(("read",)),           lambda f: f.peek(1)),
    ("readline_raises", lambda: reader(("read",)),           lambda f: f.readline()),
    ("readinto_raises", lambda: reader(("read",)),
     lambda f: f.readinto(bytearray(4096))),
    ("readinto1_raises", lambda: reader(("read",)),
     lambda f: f.readinto1(bytearray(4096))),
    ("write_flush_raises", lambda: writer(("write",), buffer_size=8),
     lambda f: f.write(b"y" * 4096)),
    ("flush_raises",    lambda: writer(("write",), buffer_size=8),
     lambda f: (f.write(b"y" * 4), f.flush())),
    ("seek_raises",     lambda: random_(("seek",), buffer_size=8),
     lambda f: (f.write(b"y" * 4), f.seek(9999))),
    ("truncate_raises", lambda: random_(("truncate",), buffer_size=8),
     lambda f: f.truncate(0)),
    ("close_flush_raises", lambda: writer(("write",), buffer_size=8),
     lambda f: (f.write(b"y" * 4096), f.close())),
    ("readinto_huge",   lambda: reader(), lambda f: f.readinto(bytearray(0))),
]


def probe_bricked(f):
    """Call something that takes ENTER_BUFFERED; report whether it is bricked."""
    for attempt in (lambda: f.seek(0), lambda: f.flush(), lambda: f.read1(1)):
        try:
            attempt()
            return None
        except Exception as exc:  # noqa: BLE001
            if BRICKED in str(exc):
                return f"{type(exc).__name__}: {exc}"
            continue
    return None


def main():
    backend = "_pyio" if USE_PYIO else "io (C)"
    print(f"backend: {backend}   python: {sys.version.split()[0]}")
    leaked = []
    for name, factory, action in SCENARIOS:
        try:
            f = factory()
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:22s} SKIP (setup: {type(exc).__name__}: {exc})")
            continue
        try:
            action(f)
            first = "no exception"
        except Exception as exc:  # noqa: BLE001
            first = f"{type(exc).__name__}"
        verdict = probe_bricked(f)
        if verdict is not None:
            leaked.append((name, verdict))
            print(f"  {name:22s} first={first:18s} *** BRICKED: {verdict}")
        else:
            print(f"  {name:22s} first={first:18s} ok (lock still takeable)")
        try:
            f.close()
        except Exception:  # noqa: BLE001, S110
            pass

    print()
    if leaked:
        print(f"RESULT: {len(leaked)} span(s) LEAKED the buffered lock")
        for name, v in leaked:
            print(f"   - {name}: {v}")
        return 1
    print(f"RESULT: 0 leaks across {len(SCENARIOS)} error-path scenarios")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)

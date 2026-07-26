"""Re-`__init__` frees the buffered lock that an in-flight span is holding.

`_buffered_init` (Modules/_io/bufferedio.c:853-856):

    if (self->lock)
        PyThread_free_lock(self->lock);
    self->lock = PyThread_allocate_lock();
    ...
    self->owner = 0;

`_io.BufferedReader.__init__` carries **no** `@critical_section` decorator and
its impl takes **no** ENTER_BUFFERED (the 12 ENTER_BUFFERED sites are at :561
:583 :943 :968 :1008 :1017 :1059 :1115 :1236 :1429 :1476 :2097 -- none is in
`_buffered_init` or any `__init__`).  So a re-`__init__` is free to destroy the
very lock a live span is holding.

Every ENTER_BUFFERED span reaches user Python through `self->raw`
(`raw.readinto()` / `raw.write()` / `raw.seek()`), so the re-init needs no
threads at all -- a raw object that calls `f.__init__(...)` from inside its own
`readinto` is enough:

    f.read()                       ENTER_BUFFERED -> self->lock = L1, owner = me
      -> raw.readinto(...)         user Python
         -> f.__init__(other)      PyThread_free_lock(L1)   <-- L1 freed HELD
                                   self->lock = L2 (fresh, UNLOCKED)
                                   self->owner = 0
      <- back in the span
    LEAVE_BUFFERED                 self->owner = 0;
                                   PyThread_release_lock(L2)  <-- never acquired

Two defects on one path:
  1. L1 is destroyed while held (freeing a locked lock).
  2. LEAVE_BUFFERED releases L2, which this thread never acquired -- so the
     lock's state is corrupted and mutual exclusion is silently lost.

The guarded twin is the rest of the file: every state-mutating buffered method
brackets itself in ENTER_BUFFERED/LEAVE_BUFFERED.  `_buffered_init` is the one
mutator that rebuilds the lock itself and therefore cannot -- which is exactly
why it needs the world to be quiet, and nothing makes it so.

Compare `Lib/_pyio.py`: its `_BufferedIOMixin.__init__` never replaces a live
`self._read_lock`, so the twin has no equivalent window.

Usage: <python> io_buffered_relock_uaf.py [--pyio] [scenario]
Best signal: a GIL ASan build (per the FT-ASan/mimalloc caveat).
"""

import sys
import threading

USE_PYIO = "--pyio" in sys.argv
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
SCENARIO = ARGS[0] if ARGS else "all"

if USE_PYIO:
    import _pyio as iomod
else:
    import io as iomod


class ReinitRaw(iomod.RawIOBase):
    """Raw stream that re-__init__s the buffered object from inside its span."""

    def __init__(self, kind="reader", rounds=1):
        self.buffered = None
        self.kind = kind
        self.rounds = rounds
        self.done = 0
        self.data = b"a" * 4096
        self.pos = 0

    def readable(self):
        return True

    def writable(self):
        return True

    def seekable(self):
        return True

    def _reinit(self):
        if self.buffered is None or self.done >= self.rounds:
            return
        self.done += 1
        fresh = ReinitRaw(self.kind, rounds=0)
        try:
            # frees self->lock, which the enclosing span is holding
            self.buffered.__init__(fresh, buffer_size=32)
        except Exception as exc:  # noqa: BLE001
            print(f"    (re-init raised {type(exc).__name__}: {exc})", flush=True)

    def readinto(self, b):
        self._reinit()
        n = min(len(b), len(self.data) - self.pos)
        if n <= 0:
            return 0
        b[:n] = self.data[self.pos:self.pos + n]
        self.pos += n
        return n

    def write(self, b):
        self._reinit()
        return len(b)

    def seek(self, pos, whence=0):
        self.pos = 0
        return 0

    def tell(self):
        return self.pos


def sc_read():
    raw = ReinitRaw("reader")
    f = iomod.BufferedReader(raw, buffer_size=32)
    raw.buffered = f
    f.read(2048)
    return f


def sc_readline():
    raw = ReinitRaw("reader")
    f = iomod.BufferedReader(raw, buffer_size=32)
    raw.buffered = f
    f.readline()
    return f


def sc_readinto():
    raw = ReinitRaw("reader")
    f = iomod.BufferedReader(raw, buffer_size=32)
    raw.buffered = f
    f.readinto(bytearray(2048))
    return f


def sc_write():
    raw = ReinitRaw("writer")
    f = iomod.BufferedWriter(raw, buffer_size=32)
    raw.buffered = f
    f.write(b"z" * 4096)
    return f


def sc_read_all():
    raw = ReinitRaw("reader")
    f = iomod.BufferedReader(raw, buffer_size=32)
    raw.buffered = f
    f.read()
    return f


SCENARIOS = {
    "read": sc_read,
    "read_all": sc_read_all,
    "readline": sc_readline,
    "readinto": sc_readinto,
    "write": sc_write,
}


def mutual_exclusion_still_holds(f):
    """After the corruption, can two threads be inside a span at once?

    LEAVE_BUFFERED released a lock this thread never took, so the lock's state
    is wrong.  Probe it: if the guard is intact one of the two threads must
    serialise behind the other.
    """
    inside = []
    overlap = []
    barrier = threading.Barrier(2, timeout=5)

    class SlowRaw(iomod.RawIOBase):
        def readable(self):
            return True

        def readinto(self, b):
            inside.append(1)
            if len(inside) > 1:
                overlap.append(1)
            try:
                barrier.wait()
            except Exception:  # noqa: BLE001, S110
                pass
            inside.pop()
            return 0

    try:
        f.__init__(SlowRaw(), buffer_size=32)
    except Exception:  # noqa: BLE001
        return None
    ts = [threading.Thread(target=lambda: f.read(64)) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    return bool(overlap)


def main():
    backend = "_pyio" if USE_PYIO else "io (C)"
    print(f"backend: {backend}  python: {sys.version.split()[0]}", flush=True)
    names = list(SCENARIOS) if SCENARIO == "all" else [SCENARIO]
    for name in names:
        print(f"  scenario {name}:", flush=True)
        try:
            f = SCENARIOS[name]()
        except Exception as exc:  # noqa: BLE001
            print(f"    outer raised {type(exc).__name__}: {exc}", flush=True)
            continue
        print("    survived the span", flush=True)
        ov = mutual_exclusion_still_holds(f)
        if ov is True:
            print("    *** MUTUAL EXCLUSION LOST: two threads inside one span",
                  flush=True)
        elif ov is False:
            print("    mutual exclusion still holds", flush=True)
        try:
            f.detach()
        except Exception:  # noqa: BLE001, S110
            pass
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

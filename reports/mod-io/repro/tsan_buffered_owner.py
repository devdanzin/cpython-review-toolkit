"""TSan stress for `buffered.owner` -- the plain field behind the re-entrancy guard.

`Modules/_io/bufferedio.c:259` declares

    volatile unsigned long owner;

`volatile` is a compiler barrier, not an atomic, and carries no cross-thread
ordering.  It is written by two plain stores that run only under `self->lock`:

    ENTER_BUFFERED :332   self->owner = PyThread_get_thread_ident()
    LEAVE_BUFFERED :336   self->owner = 0

but it is READ at :299 by a thread that has just FAILED to take that lock:

    if (self->owner == PyThread_get_thread_ident()) { ... "reentrant call" }

so the read is unsynchronised with respect to both writes.  Under free-threading
that is a plain-read/plain-write data race on the same word.

This script maximises the window: many threads hammer one shared buffered
object, so most of them fail the non-blocking `PyThread_acquire_lock(lock, 0)`
and fall into `_enter_buffered_busy`, reading `owner` while the current holder
writes it.

Run under a TSan free-threaded build and WAIT FOR EXIT -- a TSan log from a live
process is indistinguishable from a clean one.

Usage: PYTHON_GIL=0 <tsan python> tsan_buffered_owner.py [seconds]
"""

import io
import sys
import threading
import time

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
NTHREADS = 8


class SlowRaw(io.RawIOBase):
    """Raw stream slow enough to keep the span open and force contention."""

    def __init__(self):
        # MUST contain newlines and MUST report EOF: a raw stream with no '\n'
        # that wraps at EOF makes readline() loop forever.  The first version of
        # this script did exactly that and produced 7 ops in 246s with all 8
        # threads stuck -- a harness hang indistinguishable from a real one.
        self._data = (b"a" * 31 + b"\n") * 2048
        self._pos = 0

    def readable(self):
        return True

    def writable(self):
        return True

    def seekable(self):
        return True

    def readinto(self, b):
        n = min(len(b), len(self._data) - self._pos)
        if n <= 0:
            return 0                      # honest EOF -- terminates readline()
        b[:n] = self._data[self._pos:self._pos + n]
        self._pos += n
        return n

    def write(self, b):
        return len(b)

    def seek(self, pos, whence=0):
        self._pos = 0
        return 0

    def tell(self):
        return self._pos


def main():
    print(f"gil enabled: {getattr(sys, '_is_gil_enabled', lambda: 'n/a')()}",
          flush=True)
    shared = io.BufferedRandom(SlowRaw(), buffer_size=64)
    stop = threading.Event()
    counts = [0] * NTHREADS
    errs: list[str] = []

    def worker(idx):
        buf = bytearray(256)
        i = 0
        while not stop.is_set():
            try:
                op = i & 7
                if op == 0:
                    shared.read(128)
                elif op == 1:
                    shared.read1(128)
                elif op == 2:
                    shared.peek(16)
                elif op == 3:
                    shared.readinto(buf)
                elif op == 4:
                    shared.write(b"w" * 128)
                elif op == 5:
                    shared.flush()
                elif op == 6:
                    shared.seek(0)
                else:
                    shared.readline()
            except Exception as exc:  # noqa: BLE001
                if len(errs) < 20:
                    errs.append(f"{type(exc).__name__}: {exc}")
            i += 1
            counts[idx] += 1

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(NTHREADS)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    time.sleep(DURATION)
    stop.set()
    for t in threads:
        t.join(30.0)
    alive = [t for t in threads if t.is_alive()]

    print(f"elapsed {time.monotonic() - t0:.1f}s  ops={sum(counts)}  "
          f"threads_stuck={len(alive)}", flush=True)
    if errs:
        print(f"first exceptions ({len(errs)} captured):", flush=True)
        for e in dict.fromkeys(errs):
            print("   ", e, flush=True)
    try:
        shared.detach()
    except Exception:  # noqa: BLE001, S110
        pass
    print("DONE", flush=True)
    return 1 if alive else 0


if __name__ == "__main__":
    sys.exit(main())

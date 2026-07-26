"""Isolate the _pyio / _io divergence found by io_buffered_reentrancy.py.

The C accelerator guards same-thread re-entry into a buffered object with an
owner check (Modules/_io/bufferedio.c:299-303) and raises

    RuntimeError: reentrant call inside <_io.BufferedWriter>

Lib/_pyio.py's BufferedWriter locks with a plain `threading.Lock` and tracks no
owner, so the identical program deadlocks permanently instead.

Run with a timeout; the C backend exits 0 fast, the _pyio backend hangs.

Usage: <python> pyio_reentrant_hang.py [--pyio]
"""

import sys
import threading

USE_PYIO = "--pyio" in sys.argv
if USE_PYIO:
    import _pyio as iomod
else:
    import io as iomod


class ReenteringRaw(iomod.RawIOBase):
    def __init__(self):
        self.buffered = None
        self.depth = 0
        self.inner = None

    def writable(self):
        return True

    def write(self, b):
        if self.buffered is not None and not self.depth:
            self.depth += 1
            try:
                self.buffered.write(b"nested")
            except BaseException as exc:  # noqa: BLE001
                self.inner = f"{type(exc).__name__}: {exc}"
            else:
                self.inner = "no exception"
            finally:
                self.depth -= 1
        return len(b)


def main():
    backend = "_pyio" if USE_PYIO else "io (C)"
    print(f"backend: {backend}  python: {sys.version.split()[0]}", flush=True)

    raw = ReenteringRaw()
    f = iomod.BufferedWriter(raw, buffer_size=8)
    raw.buffered = f

    watchdog = threading.Timer(
        10.0,
        lambda: (print("WATCHDOG: still blocked after 10s -> DEADLOCK",
                       flush=True), __import__("os")._exit(3)),
    )
    watchdog.daemon = True
    watchdog.start()

    print("calling f.write(b'z'*64) -- forces raw.write() re-entry", flush=True)
    f.write(b"z" * 64)
    watchdog.cancel()
    print(f"returned. inner call -> {raw.inner}", flush=True)
    raw.buffered = None
    return 0


if __name__ == "__main__":
    sys.exit(main())

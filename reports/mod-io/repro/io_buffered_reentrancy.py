"""Positive control for the leak oracle + measurement of the re-entrancy guard.

Answers three questions the static audit raised:

(b) Two ENTER_BUFFERED entries in one function (_io__Buffered_read_impl :1008
    and :1017).  They sit in the two arms of `if (n == -1) {...} else {...}`, so
    only one runs per call.  If control flow DID allow both, the second acquire
    would find `self->owner == PyThread_get_thread_ident()` and take
    _enter_buffered_busy's error branch (bufferedio.c:299-303) -- a self-
    inflicted RuntimeError, NOT a deadlock, because PyThread_acquire_lock is
    called with waitflag=0 first and only the *busy* helper ever blocks.
    Scenario `same_thread_reentry` measures that claim directly.

(c) A user callback invoked INSIDE a span that re-enters the same buffered
    object hits the same owner check.  Scenario `raw_callback_reentry` drives it
    through a real raw.write()/raw.readinto() callback.

Also the POSITIVE CONTROL for io_buffered_lock_leak.py: that script concludes
"no span leaked" from the ABSENCE of `RuntimeError: reentrant call inside`.
That conclusion is only worth anything if the string is reachable at all.  Here
we reach it on purpose.

Usage:  <python> io_buffered_reentrancy.py [--pyio]
Exit 0 = the guard behaved as documented (error raised, no hang, not bricked).
"""

import sys
import threading

USE_PYIO = "--pyio" in sys.argv
if USE_PYIO:
    import _pyio as iomod
else:
    import io as iomod

BRICKED = "reentrant call inside"
results = []


def record(name, outcome, detail=""):
    results.append((name, outcome, detail))
    print(f"  {name:24s} {outcome:14s} {detail}")


class ReenteringRaw(iomod.RawIOBase):
    """Raw stream whose write()/readinto() re-enters the buffered object."""

    def __init__(self):
        self.buffered = None
        self.inner = None
        self.depth = 0

    def readable(self):
        return True

    def writable(self):
        return True

    def seekable(self):
        return True

    def _reenter(self):
        if self.buffered is None or self.depth:
            return
        self.depth += 1
        try:
            self.buffered.write(b"nested")
        except BaseException as exc:  # noqa: BLE001
            self.inner = f"{type(exc).__name__}: {exc}"
        else:
            self.inner = "no exception"
        finally:
            self.depth -= 1

    def write(self, b):
        self._reenter()
        return len(b)

    def readinto(self, b):
        self._reenter()
        return 0


def raw_callback_reentry():
    """A user raw.write() that calls back into the same BufferedWriter."""
    raw = ReenteringRaw()
    f = iomod.BufferedWriter(raw, buffer_size=8)
    raw.buffered = f
    try:
        f.write(b"z" * 64)          # forces raw.write() -> re-entry
        outer = "no exception"
    except BaseException as exc:    # noqa: BLE001
        outer = f"{type(exc).__name__}"
    inner = raw.inner
    raw.buffered = None             # stop re-entering so we can probe
    if inner and BRICKED in inner:
        record("raw_callback_reentry", "GUARD-FIRED", f"inner={inner[:60]}")
    else:
        record("raw_callback_reentry", "no-guard", f"inner={inner} outer={outer}")
    # the guard must not have bricked the object
    try:
        f.flush()
        record("  post-reentry usable", "ok")
    except BaseException as exc:    # noqa: BLE001
        state = "BRICKED" if BRICKED in str(exc) else "other"
        record("  post-reentry usable", state, str(exc)[:60])
    try:
        f.detach()
    except BaseException:           # noqa: BLE001, S110
        pass


def same_thread_reentry():
    """Re-enter read() from inside a raw readinto() -- the (b) claim."""
    raw = ReenteringRaw()
    f = iomod.BufferedReader(raw, buffer_size=8)

    def reenter_read():
        try:
            f.read(4)
        except BaseException as exc:  # noqa: BLE001
            raw.inner = f"{type(exc).__name__}: {exc}"
        else:
            raw.inner = "no exception"

    raw._reenter = reenter_read       # type: ignore[method-assign]
    try:
        f.read()
    except BaseException as exc:      # noqa: BLE001
        pass
    inner = raw.inner
    if inner and BRICKED in inner:
        record("same_thread_reentry", "GUARD-FIRED", f"inner={inner[:60]}")
    else:
        record("same_thread_reentry", "no-guard", f"inner={inner}")


def cross_thread_contention():
    """A second THREAD must block-and-proceed, never see 'reentrant call'."""
    raw = ReenteringRaw()
    f = iomod.BufferedWriter(raw, buffer_size=8)
    gate = threading.Event()
    out = {}

    def slow_write(b):
        gate.set()
        threading.Event().wait(0.25)   # hold the span
        return len(b)

    raw.write = slow_write             # type: ignore[method-assign]

    def other():
        gate.wait(2.0)
        try:
            f.write(b"q" * 64)
            out["other"] = "ok"
        except BaseException as exc:   # noqa: BLE001
            out["other"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=other)
    t.start()
    try:
        f.write(b"z" * 64)
    except BaseException as exc:       # noqa: BLE001
        out["main"] = f"{type(exc).__name__}"
    t.join(10.0)
    if t.is_alive():
        record("cross_thread_contention", "HUNG", "second thread never returned")
    elif BRICKED in str(out.get("other", "")):
        record("cross_thread_contention", "WRONG-ERROR", str(out.get("other"))[:60])
    else:
        record("cross_thread_contention", "ok", f"other={out.get('other')}")
    try:
        f.detach()
    except BaseException:              # noqa: BLE001, S110
        pass


def main():
    backend = "_pyio" if USE_PYIO else "io (C)"
    print(f"backend: {backend}   python: {sys.version.split()[0]}   "
          f"gil={getattr(sys, '_is_gil_enabled', lambda: 'n/a')()}")
    raw_callback_reentry()
    same_thread_reentry()
    cross_thread_contention()
    print()
    bad = [r for r in results if r[1] in ("HUNG", "BRICKED", "WRONG-ERROR")]
    guard = [r for r in results if r[1] == "GUARD-FIRED"]
    print(f"guard fired in {len(guard)} scenario(s); {len(bad)} anomaly(ies)")
    if not guard:
        print("WARNING: the 'reentrant call inside' branch was never reached -- "
              "the leak oracle in io_buffered_lock_leak.py is UNVALIDATED here")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

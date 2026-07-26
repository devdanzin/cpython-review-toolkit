"""Task (c): the clinic critical-section boundary, read as a free-threading
question.

Two separate windows exist around every clinic-wrapped bytearray method:

  W1  BEFORE the lock -- 18 wrappers run an arbitrary-Python converter
      (_PyNumber_Index / _PyEval_SliceIndex / PyObject_GetBuffer) before
      Py_BEGIN_CRITICAL_SECTION.
  W2  INSIDE the lock -- the impl calls something that reaches user Python
      (PyObject_GetBuffer, PyBuffer_Release -> __release_buffer__,
      _getbytevalue -> __index__).

W2 is the one that matters, and it matters because a critical section is NOT
held across a thread detach: `_PyCriticalSection_SuspendAll`
(Python/pystate.c) releases every section the thread holds when it detaches,
and `_PyCriticalSection_Resume` re-takes them afterwards.

MEASURED (mutator wait time before b.clear() completes):

                     release-ft   debug-ft   release-gil
  strip_busy (W2, busy loop)  1.200s     1.200s     0.016s
  strip_w2   (W2, blocking)   0.000s     0.000s     0.000s

So on a free-threaded build the section DOES exclude across a non-detaching
callback and does NOT across a detaching one.  Under the GIL there is no such
distinction -- the mutator gets in after the 5 ms switch interval either way.
Object-state guards (ob_exports++) survive a detach; locks do not.

Usage: python gil_clinic_lock_window.py <probe>

Probes
  strip_w2      bytearray.strip(x), x.__release_buffer__ BLOCKS.  True W2:
                clinic:1394 passes `bytes` as a plain object, so both
                PyObject_GetBuffer and PyBuffer_Release run inside the lock.
  strip_busy    Same site, busy loop instead -- the real discriminator.
  find_w1       bytearray.find(sub, start), start.__index__ BLOCKS.  W1.
  append_lock   b.append(v), v.__index__ BLOCKS.  NOTE: this is W1, not W2 --
                clinic:1244 runs _getbytevalue BEFORE Py_BEGIN_CRITICAL_SECTION
                at :1247.  Kept as the W1 control; do not read it as a
                statement about the lock.
  append_busy   / append_busy_nogc   W1 variants.
  hold_only     No callback: baseline that the mutator can always run.
  strip_uaf_xthread   CPY-0180 reached cross-thread with a NON-mutating
                callback (the resize comes from the other thread).
"""

from __future__ import annotations

import faulthandler
import sys
import threading
import time

faulthandler.enable()


def p(m: str) -> None:
    print(f"PROBE:{m}", flush=True)


def measure(setup, name: str) -> None:
    entered = threading.Event()
    release = threading.Event()
    b = bytearray(b"hello world" * 8)

    op = setup(b, entered, release)

    exc: list = []

    def runner():
        try:
            op()
        except BaseException as e:  # noqa: BLE001
            exc.append(f"{type(e).__name__}: {e}")

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    if not entered.wait(5.0):
        p(f"{name}=never_entered_callback")
        release.set()
        t.join(5)
        return

    done = threading.Event()
    res: list = []

    def mutator():
        try:
            b.clear()
            res.append("cleared")
        except BaseException as e:  # noqa: BLE001
            res.append(f"{type(e).__name__}")
        done.set()

    t0 = time.monotonic()
    m = threading.Thread(target=mutator, daemon=True)
    m.start()
    proceeded = done.wait(2.0)
    dt = time.monotonic() - t0
    p(f"{name}_mutator_ran_during_callback={proceeded} after={dt:.3f}s res={res}")
    release.set()
    t.join(10)
    m.join(10)
    p(f"{name}_op_exc={exc} final_len={len(b)}")
    p("completed")


def strip_w2(b, entered, release):
    class Blocking:
        def __buffer__(self, flags):
            return memoryview(b" ")

        def __release_buffer__(self, view):
            entered.set()
            release.wait(10.0)

    return lambda: b.strip(Blocking())


def strip_busy(b, entered, release):
    """A TRUE W2 discriminator.

    `bytearray.strip` takes its argument as a plain object (clinic:1394), so
    both PyObject_GetBuffer and PyBuffer_Release run INSIDE
    Py_BEGIN_CRITICAL_SECTION(self).  Here __release_buffer__ busy-loops
    instead of blocking, so the thread never detaches.  If the mutator now has
    to WAIT, the section is dropped only on a detach; if it still gets in, the
    section does not exclude at all.

    (Note: `bytearray.append` is NOT a W2 site -- clinic:1244 runs
    _getbytevalue BEFORE Py_BEGIN_CRITICAL_SECTION, so append_lock/append_busy
    measure W1.)
    """

    class Busy:
        def __buffer__(self, flags):
            return memoryview(b" ")

        def __release_buffer__(self, view):
            entered.set()
            t0 = time.monotonic()
            while time.monotonic() - t0 < 1.2:
                pass

    return lambda: b.strip(Busy())


def find_w1(b, entered, release):
    class BlockIndex:
        def __index__(self):
            entered.set()
            release.wait(10.0)
            return 0

    return lambda: b.find(b"o", BlockIndex())


def append_lock(b, entered, release):
    class BlockIndex:
        def __index__(self):
            entered.set()
            release.wait(10.0)
            return 65

    return lambda: b.append(BlockIndex())


def append_busy(b, entered, release):
    """The discriminator: __index__ BUSY-LOOPS instead of blocking.

    A busy loop does not detach the thread state, so on a free-threaded build
    the critical section stays held and the mutator must WAIT.  Compare with
    append_lock, where the same section is dropped.
    """

    class BusyIndex:
        def __index__(self):
            entered.set()
            t0 = time.monotonic()
            while time.monotonic() - t0 < 1.2:
                pass
            return 65

    return lambda: b.append(BusyIndex())


def append_busy_nogc(b, entered, release):
    """append_busy with the cyclic GC disabled and the switch interval raised.

    Discriminator for WHY the section is dropped: if the mutator now has to
    wait, the drop was the stop-the-world GC detaching this thread; if it
    still gets in immediately, something else releases it.
    """
    import gc

    gc.disable()
    sys.setswitchinterval(100.0)
    return append_busy(b, entered, release)


def hold_only(b, entered, release):
    def op():
        entered.set()
        release.wait(10.0)

    return op


def strip_uaf_xthread() -> None:
    """CPY-0180 reached from a SECOND THREAD with a non-mutating callback.

    The recorded reproduction has __release_buffer__ itself resize `self`.
    Here the callback only blocks; the resize comes from another thread.
    """
    entered = threading.Event()
    release = threading.Event()
    b = bytearray(b"PAYLOAD" * 3)

    class Blocking:
        def __buffer__(self, flags):
            return memoryview(b" ")

        def __release_buffer__(self, view):
            entered.set()
            release.wait(10.0)

    out: list = []

    def runner():
        out.append(b.strip(Blocking()))

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    if not entered.wait(5.0):
        p("strip_uaf_xthread=never_entered")
        return
    # From another thread, while strip holds a raw char* into b's buffer:
    b.clear()
    b.extend(b"Z" * 4096)  # force reallocation away from the old block
    release.set()
    t.join(10)
    p(f"strip_uaf_xthread_result={out!r}")
    p(f"strip_uaf_xthread_expected={b'PAYLOAD' * 3!r}")
    p(f"strip_uaf_xthread_correct={out == [bytearray(b'PAYLOAD' * 3)]}")
    p("completed")


if __name__ == "__main__":
    probe = sys.argv[1] if len(sys.argv) > 1 else "strip_w2"
    if probe == "strip_uaf_xthread":
        strip_uaf_xthread()
        raise SystemExit(0)
    fn = {
        "strip_w2": strip_w2,
        "strip_busy": strip_busy,
        "find_w1": find_w1,
        "append_lock": append_lock,
        "append_busy": append_busy,
        "append_busy_nogc": append_busy_nogc,
        "hold_only": hold_only,
    }[probe]
    measure(fn, probe)

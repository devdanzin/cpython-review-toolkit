#!/usr/bin/env python3
"""lock-discipline-checker, slice obj-sequences.

`bytearray.__iadd__` takes ONE object's critical section and reaches the second
object's lock nested, through PyObject_GetBuffer:

    bytearray_iconcat            bytearrayobject.c:382   Py_BEGIN_CRITICAL_SECTION(op)
      bytearray_iconcat_lock_held:356  PyObject_GetBuffer(other)
        bytearray_getbuffer      bytearrayobject.c:77    Py_BEGIN_CRITICAL_SECTION(other)
          bytearray_getbuffer_lock_held:69                other->ob_exports++
        <-- other's section ENDS here; the export stays up
      bytearray_resize_lock_held:368 -> _canresize(self) reads SELF->ob_exports

Because the inner section is released as soon as getbuffer returns, thread B's
transient export on b1 is visible to thread A's _canresize(b1).  Two threads
running `b1 += b2` and `b2 += b1` therefore make EACH OTHER fail with
BufferError -- an exception neither operation can raise single-threaded and
which cannot occur under the GIL.

The guarded twins are in the same file and take BOTH locks up front, so the two
operations serialise instead of interleaving:

    bytearray_ass_subscript  bytearrayobject.c:893  Py_BEGIN_CRITICAL_SECTION2(op, values)
    bytearray_mod            bytearrayobject.c:2865 Py_BEGIN_CRITICAL_SECTION2(v, w)

`b1[len(b1):] = b2` is the same byte-level operation as `b1 += b2` and routes
through the CS2 twin, so it is the control.

Reports the BufferError RATE, not just presence: threads keep running after a
failure instead of dying on the first one.

Usage: python lock_iconcat_spurious_buffererror.py <scenario>
"""

import operator
import os
import sys
import threading
import time

DURATION = float(os.environ.get("LOI_DURATION", "5.0"))
NTHREADS = int(os.environ.get("LOI_THREADS", "4"))

stop = threading.Event()
lock = threading.Lock()
tally = {"ok": 0, "buffererror": 0, "other": 0}
other_kinds = {}


def _run(fn):
    ok = be = ot = 0
    kinds = {}
    while not stop.is_set():
        try:
            fn()
            ok += 1
        except BufferError:
            be += 1
        except BaseException as e:  # noqa: BLE001
            ot += 1
            kinds[type(e).__name__] = kinds.get(type(e).__name__, 0) + 1
    with lock:
        tally["ok"] += ok
        tally["buffererror"] += be
        tally["other"] += ot
        for k, v in kinds.items():
            other_kinds[k] = other_kinds.get(k, 0) + v


def drive(fns):
    ts = [threading.Thread(target=_run, args=(fns[i % len(fns)],), daemon=True)
          for i in range(NTHREADS)]
    for t in ts:
        t.start()
    time.sleep(DURATION)
    stop.set()
    for t in ts:
        t.join(timeout=30)
    total = tally["ok"] + tally["buffererror"] + tally["other"]
    rate = (tally["buffererror"] / total * 100) if total else 0.0
    print(f"PROBE:ok={tally['ok']}")
    print(f"PROBE:buffererror={tally['buffererror']}")
    print(f"PROBE:other={tally['other']} {other_kinds}")
    print(f"PROBE:total={total}")
    print(f"PROBE:buffererror_pct={rate:.4f}")
    return 0


def s_iconcat():
    """The nested single-lock path: b1 += b2  vs  b2 += b1."""
    b1 = bytearray(b"A" * 64)
    b2 = bytearray(b"B" * 64)

    def fwd():
        operator.iadd(b1, b2)
        del b1[64:]

    def rev():
        operator.iadd(b2, b1)
        del b2[64:]

    return drive([fwd, rev])


def s_setslice():
    """CONTROL -- the Py_BEGIN_CRITICAL_SECTION2 twin, same bytes moved."""
    b1 = bytearray(b"A" * 64)
    b2 = bytearray(b"B" * 64)

    def fwd():
        b1[len(b1):] = b2
        del b1[64:]

    def rev():
        b2[len(b2):] = b1
        del b2[64:]

    return drive([fwd, rev])


def s_extend():
    """CONTROL -- bytearray.extend, clinic @critical_section, single lock,
    but it materialises the argument through its own iterator rather than a
    buffer export of the other bytearray."""
    b1 = bytearray(b"A" * 64)
    b2 = bytearray(b"B" * 64)

    def fwd():
        b1.extend(b2)
        del b1[64:]

    def rev():
        b2.extend(b1)
        del b2[64:]

    return drive([fwd, rev])


def s_iconcat_solo():
    """CONTROL -- same call volume, one thread."""
    global NTHREADS
    NTHREADS = 1
    b1 = bytearray(b"A" * 64)
    b2 = bytearray(b"B" * 64)

    def fwd():
        operator.iadd(b1, b2)
        del b1[64:]

    return drive([fwd])


def s_iconcat_disjoint():
    """CONTROL -- no shared pair: each thread owns its own two bytearrays.
    Isolates 'contention on the pair' from 'concurrency at all'."""
    pairs = [(bytearray(b"A" * 64), bytearray(b"B" * 64)) for _ in range(NTHREADS)]
    idx = [0]

    def mk(i):
        a, b = pairs[i]

        def f():
            operator.iadd(a, b)
            del a[64:]

        return f

    fns = [mk(i) for i in range(NTHREADS)]
    del idx
    return drive(fns)


SCEN = {k[2:]: v for k, v in sorted(globals().items()) if k.startswith("s_")}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in SCEN:
        print("scenarios:", " ".join(sorted(SCEN)))
        return 2
    print(f"PROBE:scenario={sys.argv[1]}")
    return SCEN[sys.argv[1]]()


if __name__ == "__main__":
    sys.exit(main())

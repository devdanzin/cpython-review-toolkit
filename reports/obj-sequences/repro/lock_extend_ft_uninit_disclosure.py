#!/usr/bin/env python3
"""lock-discipline-checker, slice obj-sequences.

REACHABILITY ESCALATION for gh-153578 (`bytearray_setslice` clamps `hi` but not
`lo`; PR #153579 open at the review ref).  The recorded reproduction needs an
adversarial object whose `__buffer__` re-enters and shrinks the receiver.  On a
FREE-THREADED build no adversarial object is needed at all: two plain
bytearrays and two ordinary threads suffice.

Mechanism -- and it is the nested-lock design, not the callback:

  bytearray_extend_impl:2186   bytearray_setslice(self, Py_SIZE(self), Py_SIZE(self), other)
                               ^^^^^^^^^^^^^^^^^  lo and hi are evaluated HERE
  bytearray_setslice:666       PyObject_GetBuffer(values)
    -> bytearray_getbuffer:77  Py_BEGIN_CRITICAL_SECTION(values)
       contended -> PyMutex_Lock parks -> _PY_LOCK_DETACH
       -> _PyThreadState_Detach -> _PyCriticalSection_SuspendAll
       -> SELF'S CRITICAL SECTION IS RELEASED, another thread shrinks self
  bytearray_setslice:673-678   hi is clamped to the NEW Py_SIZE(self); lo is NOT
  bytearray_setslice_linear:556  avail = hi - lo   ->  NEGATIVE

Then, with `avail < 0`:
  :560  assert(avail >= 0)                       -> SIGABRT on a debug build
  :607  growth = bytes_len - avail > bytes_len   -> self grown to lo + bytes_len
  :626  memmove(..., Py_SIZE(self) - lo - bytes_len)  -> count 0, moves nothing
  :631  memcpy(buf + lo, bytes, bytes_len)       -> writes only the tail
  => the byte range [hi, lo) is never written: uninitialised heap, handed to
     Python, on a release build with no crash.

The guarded twin is in the same file and was added by the same upstream sweep:
`bytearray_ass_subscript` (bytearrayobject.c:893) takes
Py_BEGIN_CRITICAL_SECTION2(op, values) when `values` is a bytearray, so the two
threads serialise and the suspend window never opens.  `b1[len(b1):] = b2` is
the same byte operation through that twin and is the control.

Scenarios:
  extend            b1.extend(b2) || b2.extend(b1)     -- the finding
  setslice          b1[len(b1):] = b2 || ...           -- CS2 twin control
  extend_solo       one thread                         -- workload control
  extend_disjoint   per-thread pairs, no sharing       -- contention control

Usage: python lock_extend_ft_uninit_disclosure.py <scenario>
"""

import os
import sys
import threading
import time

DURATION = float(os.environ.get("LOI_DURATION", "5.0"))
NTHREADS = int(os.environ.get("LOI_THREADS", "4"))

# Only these two byte values are ever written by the program.
A, B = 0x41, 0x42
SEED = 64

stop = threading.Event()
lock = threading.Lock()
tally = {"ok": 0, "buffererror": 0, "foreign": 0, "other": 0}
samples = []


def check(ba):
    """Return True if `ba` contains a byte the program never wrote."""
    bad = set(bytes(ba)) - {A, B}
    if bad:
        with lock:
            if len(samples) < 3:
                samples.append(sorted(bad)[:8])
        return True
    return False


def worker(fn):
    ok = be = fo = ot = 0
    while not stop.is_set():
        try:
            r = fn()
            ok += 1
            if r:
                fo += 1
        except BufferError:
            be += 1
        except BaseException:  # noqa: BLE001
            ot += 1
    with lock:
        tally["ok"] += ok
        tally["buffererror"] += be
        tally["foreign"] += fo
        tally["other"] += ot


def drive(fns):
    ts = [threading.Thread(target=worker, args=(fns[i % len(fns)],), daemon=True)
          for i in range(NTHREADS)]
    for t in ts:
        t.start()
    time.sleep(DURATION)
    stop.set()
    for t in ts:
        t.join(timeout=30)
    total = sum(tally.values())
    print(f"PROBE:ok={tally['ok']} buffererror={tally['buffererror']} "
          f"other={tally['other']} total={total}")
    print(f"PROBE:foreign_byte_results={tally['foreign']}")
    print(f"PROBE:foreign_samples={samples}")
    print(f"PROBE:verdict={'DISCLOSURE' if tally['foreign'] else 'clean'}")
    return 0


def _pair():
    return bytearray(bytes([A]) * SEED), bytearray(bytes([B]) * SEED)


def s_extend():
    b1, b2 = _pair()

    def fwd():
        b1.extend(b2)
        bad = check(b1)
        del b1[SEED:]
        return bad

    def rev():
        b2.extend(b1)
        bad = check(b2)
        del b2[SEED:]
        return bad

    return drive([fwd, rev])


def s_setslice():
    b1, b2 = _pair()

    def fwd():
        b1[len(b1):] = b2
        bad = check(b1)
        del b1[SEED:]
        return bad

    def rev():
        b2[len(b2):] = b1
        bad = check(b2)
        del b2[SEED:]
        return bad

    return drive([fwd, rev])


def s_extend_solo():
    global NTHREADS
    NTHREADS = 1
    return s_extend()


def s_extend_disjoint():
    pairs = [_pair() for _ in range(NTHREADS)]

    def mk(i):
        b1, b2 = pairs[i]

        def f():
            b1.extend(b2)
            bad = check(b1)
            del b1[SEED:]
            return bad

        return f

    return drive([mk(i) for i in range(NTHREADS)])


SCEN = {k[2:]: v for k, v in sorted(globals().items()) if k.startswith("s_")}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in SCEN:
        print("scenarios:", " ".join(sorted(SCEN)))
        return 2
    print(f"PROBE:scenario={sys.argv[1]}")
    return SCEN[sys.argv[1]]()


if __name__ == "__main__":
    sys.exit(main())

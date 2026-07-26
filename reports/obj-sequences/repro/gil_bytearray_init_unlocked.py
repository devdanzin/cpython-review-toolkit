"""`bytearray.__init__` is the one Python-visible bytearray mutator that runs
with NO per-object critical section.  It is correct only because the GIL
serialises it.

Objects/clinic/bytearrayobject.c.h:102 calls bytearray___init___impl with no
Py_BEGIN_CRITICAL_SECTION -- it is the ONLY one of the 35 clinic-generated
bytearray entry points without one (34 have it).  The impl body has none
either.  It then performs, unsynchronised, on a live Python-reachable object:

    :924-927   read/write self->ob_bytes_object, bytearray_reinit_from_bytes,
               self->ob_exports = 0
    :930       read Py_SIZE(self)
    :971-972   self->ob_bytes_object = encoded;  bytearray_reinit_from_bytes(...)
    :1094-1100 the append fast path:
                   if (Py_SIZE(self) + 1 < self->ob_alloc) {
                       Py_SET_SIZE(self, Py_SIZE(self) + 1);
                       PyByteArray_AS_STRING(self)[Py_SIZE(self)] = '\\0';
                   }
                   ...
                   PyByteArray_AS_STRING(self)[Py_SIZE(self)-1] = value;

Under the GIL those three statements at :1094-1100 are atomic: the only
thread-switch point in the loop is `iternext(it)` at :1076, which is above
them.  Under free threading they are a lost-update read-modify-write on
ob_size plus two writes through a concurrently-reallocated ob_start.

Guarded twin: every other mutator.  bytearray_append_impl, _extend_impl,
_insert_impl, _pop_impl, _remove_impl, _resize_impl and the sq_/mp_ slots all
run inside Py_BEGIN_CRITICAL_SECTION(self); PyByteArray_Resize (:293) takes it
too, which is why __init__'s *resize* steps are safe and the statements
between them are not.

Usage: python gil_bytearray_init_unlocked.py <probe> [seconds]

Probes
  init_vs_append   __init__(iterable) racing append()/clear()
  init_vs_init     two threads calling b.__init__(iterable)
  init_vs_read     __init__(iterable) racing bytes(b)/len(b)
  init_solo        the same call volume, single thread   (workload control)
  append_vs_append append() racing append()  (locked-mutator control)
"""

from __future__ import annotations

import faulthandler
import sys
import threading
import time

faulthandler.enable()

CHUNK = 4000


def gen(n=CHUNK):
    # A generator: every next() is a real Python frame, so the C loop at
    # bytearrayobject.c:1072-1101 yields between appends on every build.
    for i in range(n):
        yield i & 0xFF


def run(probe: str, seconds: float) -> None:
    b = bytearray(64)
    stop = threading.Event()
    errs: list[str] = []

    def initer():
        while not stop.is_set():
            try:
                b.__init__(gen())
            except Exception as e:
                errs.append(type(e).__name__)

    def appender():
        while not stop.is_set():
            try:
                for _ in range(200):
                    b.append(0x41)
                b.clear()
            except Exception as e:
                errs.append(type(e).__name__)

    def reader():
        while not stop.is_set():
            try:
                v = bytes(b)
                if len(v) != len(b):
                    pass
            except Exception as e:
                errs.append(type(e).__name__)

    roles = {
        "init_vs_append": [initer, appender, initer, appender],
        "init_vs_init": [initer, initer, initer, initer],
        "init_vs_read": [initer, reader, initer, reader],
        "append_vs_append": [appender, appender, appender, appender],
    }
    if probe == "init_solo":
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            b.__init__(gen())
        print(f"PROBE:{probe} len={len(b)} completed", flush=True)
        return

    fns = roles[probe]
    ts = [threading.Thread(target=f, daemon=True) for f in fns]
    for t in ts:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in ts:
        t.join(10)
    from collections import Counter

    print(f"PROBE:{probe} len={len(b)} errs={dict(Counter(errs))}", flush=True)
    print("PROBE:completed", flush=True)


if __name__ == "__main__":
    pr = sys.argv[1] if len(sys.argv) > 1 else "init_vs_append"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
    run(pr, secs)

"""_buffered_init frees the PyThread_type_lock a GIL-released thread is parked on.

The (c) hazard in its sharpest form.  Three threads:

  T1   buf.read()   -> ENTER_BUFFERED acquires L1, parks inside raw.readinto()
                       (user Python), still holding L1.
  T2   buf.read()   -> non-blocking acquire fails -> _enter_buffered_busy
                       (bufferedio.c:295) -> Py_BEGIN_ALLOW_THREADS (:306) ->
                       PyThread_acquire_lock(L1, 1) (:308).  GIL is RELEASED,
                       so T3 can run.
  T3   buf.__init__ -> _buffered_init (bufferedio.c:838) -> :854
                       PyThread_free_lock(L1)   <-- frees the lock T2 is
                       parked on; :855 installs a fresh L2.

__init__ is one of the eight clinic blocks in bufferedio.c with no
@critical_section, and it never goes through ENTER_BUFFERED, so neither the
buffered lock nor the free-threaded per-object lock excludes it.

Then T1 wakes and LEAVE_BUFFERED (bufferedio.c:334) releases self->lock --
now L2, which this thread never acquired.

Usage:  python io_buffered_free_lock_under_waiter.py [io|pyio]
"""

import faulthandler
import os
import sys
import threading
import time

backend = sys.argv[1] if len(sys.argv) > 1 else "io"
if backend == "pyio":
    import _pyio as io
else:
    import io

holder_parked = threading.Event()
release_holder = threading.Event()
BUFSIZE = 8192


class Holder(io.RawIOBase):
    def readable(self):
        return True

    def readinto(self, b):
        holder_parked.set()
        release_holder.wait(20)
        b[0] = 65
        return 1


class Plain(io.RawIOBase):
    def readable(self):
        return True

    def readinto(self, b):
        b[0] = 66
        return 1


buf = io.BufferedReader(Holder(), buffer_size=BUFSIZE)
results = []


def t1():
    try:
        buf.read(1)
        results.append("t1-ok")
    except BaseException as e:
        results.append(f"t1-{type(e).__name__}")


def t2():
    try:
        buf.read(1)                       # parks on L1 with the GIL released
        results.append("t2-ok")
    except BaseException as e:
        results.append(f"t2-{type(e).__name__}")


threading.Thread(target=t1, daemon=True).start()
holder_parked.wait(10)
threading.Thread(target=t2, daemon=True).start()
time.sleep(1.0)                            # let t2 reach bufferedio.c:308

print(f"{backend}: freeing the lock t2 is parked on", flush=True)
try:
    buf.__init__(Plain(), buffer_size=BUFSIZE)   # -> PyThread_free_lock(L1)
    print(f"{backend}: __init__ returned", flush=True)
except BaseException as e:
    print(f"{backend}: __init__ raised {type(e).__name__}: {e}", flush=True)

release_holder.set()
time.sleep(2.0)
print(f"{backend}: results={results}", flush=True)
if len(results) < 2:
    print(f"{backend}: THREADS WEDGED", flush=True)
    faulthandler.dump_traceback()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(7)
sys.stdout.flush()
os._exit(0)

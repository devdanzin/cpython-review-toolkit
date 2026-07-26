"""AB-BA lock-order inversion between two _io.BufferedReader objects.

_io's per-object lock is a raw PyThread_type_lock taken by ENTER_BUFFERED
(Modules/_io/bufferedio.c:329).  A buffered method holds that lock across a
dispatch into `self->raw`, which for a Python-level raw object is arbitrary
user code.  If that user code enters a *second* buffered object, the thread
holds two _io locks, acquired in the order it happened to visit them.

Two threads visiting the same two objects in opposite orders deadlock.  The
block happens inside _enter_buffered_busy's Py_BEGIN_ALLOW_THREADS region
(bufferedio.c:306-318), so the GIL is released and the rest of the
interpreter keeps running -- only the two threads are wedged, permanently,
and PyThread_acquire_lock(lock, 1) passes intr_flag=0 so no signal breaks it.

Usage:  python io_buffered_lock_inversion.py [io|pyio]
Exit codes: 0 = both threads finished (no deadlock), 7 = deadlock (timeout).
"""

import faulthandler
import sys
import threading
import time

backend = sys.argv[1] if len(sys.argv) > 1 else "io"
if backend == "pyio":
    import _pyio as io
else:
    import io

DEADLINE = 10.0

start = threading.Barrier(2)
done = []


class Raw(io.RawIOBase):
    """A raw stream whose readinto() re-enters a *different* buffered object."""

    def __init__(self):
        self.other = None
        self.hops = 0

    def readable(self):
        return True

    def readinto(self, b):
        # Only cross over on the first hop, so the chain terminates.
        if self.other is not None and self.hops == 0:
            self.hops = 1
            time.sleep(0.05)          # widen the interleaving window
            self.other.read(1)        # <-- takes the OTHER buffered lock
        b[0] = 65
        return 1


rawA, rawB = Raw(), Raw()
bufA = io.BufferedReader(rawA, buffer_size=8)
bufB = io.BufferedReader(rawB, buffer_size=8)
rawA.other = bufB     # A's raw reaches into B
rawB.other = bufA     # B's raw reaches into A


def worker(name, first):
    start.wait()
    first.read(1)
    done.append(name)


t1 = threading.Thread(target=worker, args=("A->B", bufA), daemon=True)
t2 = threading.Thread(target=worker, args=("B->A", bufB), daemon=True)
t1.start()
t2.start()

t1.join(DEADLINE)
t2.join(0.5)

if len(done) == 2:
    print(f"{backend}: OK, both threads completed: {done}")
    sys.exit(0)

print(f"{backend}: DEADLOCK -- completed={done} "
      f"t1.alive={t1.is_alive()} t2.alive={t2.is_alive()}")
print("--- faulthandler dump of all threads ---", flush=True)
faulthandler.dump_traceback()
sys.stdout.flush()
sys.stderr.flush()
# Threads are daemon; _exit avoids hanging in interpreter finalization
# (which would itself block in _enter_buffered_busy's grace period).
import os
os._exit(7)

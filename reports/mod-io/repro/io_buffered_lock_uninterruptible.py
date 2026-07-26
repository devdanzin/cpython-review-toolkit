"""_io's buffered lock is NOT interruptible; _pyio's RLock is.

Setup: a worker thread grabs bufA's lock and parks inside rawA.readinto().
The MAIN thread then calls bufA.read(), fails the non-blocking acquire, and
blocks in _enter_buffered_busy (Modules/_io/bufferedio.c:308):

    st = PyThread_acquire_lock(self->lock, 1);

PyThread_acquire_lock() is PyThread_acquire_lock_timed(lock, -1, /*intr=*/0)
-- intr_flag 0 means a signal does NOT break the wait.  SIGINT is recorded by
the C signal handler but the main thread never returns to the eval loop, so
KeyboardInterrupt is never raised.

_pyio uses threading.RLock, whose acquire() passes intr_flag=1 on the main
thread, so the same wait is broken by SIGINT.

Usage:  python io_buffered_lock_uninterruptible.py [io|pyio]
Exit codes: 0 = KeyboardInterrupt delivered (interruptible)
            8 = SIGINT ignored, main thread still wedged (uninterruptible)
"""

import faulthandler
import os
import signal
import sys
import threading
import time

backend = sys.argv[1] if len(sys.argv) > 1 else "io"
if backend == "pyio":
    import _pyio as io
else:
    import io

holder_in = threading.Event()
release = threading.Event()
sigint_seen = []


class Raw(io.RawIOBase):
    def readable(self):
        return True

    def readinto(self, b):
        holder_in.set()
        release.wait(30)      # park here, still holding bufA's buffered lock
        b[0] = 65
        return 1


raw = Raw()
buf = io.BufferedReader(raw, buffer_size=8)


def holder():
    buf.read(1)


def sigint_sender():
    # main thread should be wedged in the buffered-lock acquire by now
    time.sleep(2.0)
    os.kill(os.getpid(), signal.SIGINT)
    time.sleep(3.0)
    # still alive => the signal did not break the wait
    sigint_seen.append("ignored")
    print(f"{backend}: SIGINT IGNORED -- main thread still blocked after 3s")
    print("--- faulthandler dump of all threads ---", flush=True)
    faulthandler.dump_traceback()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(8)


threading.Thread(target=holder, daemon=True).start()
holder_in.wait(10)
threading.Thread(target=sigint_sender, daemon=True).start()

t0 = time.monotonic()
try:
    buf.read(1)          # blocks: worker holds the lock
    print(f"{backend}: read returned after {time.monotonic()-t0:.2f}s "
          f"(no interrupt needed)", flush=True)
except KeyboardInterrupt:
    print(f"{backend}: KeyboardInterrupt delivered after "
          f"{time.monotonic()-t0:.2f}s -- INTERRUPTIBLE", flush=True)
    release.set()
    os._exit(0)
release.set()
os._exit(0)

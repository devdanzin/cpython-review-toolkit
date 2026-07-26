"""(d) self->owner is read outside every lock that protects its writes.

Modules/_io/bufferedio.c:

  :258  volatile unsigned long owner;          /* not _Py_atomic anything */
  :299  if (self->owner == PyThread_get_thread_ident())   <-- plain READ,
                                                              no lock held
  :332  (self->owner = PyThread_get_thread_ident(), 1)    <-- write, under
                                                              the buffered lock
  :336  self->owner = 0;                                  <-- write, under
                                                              the buffered lock

The read at :299 is the reentrancy check inside _enter_buffered_busy, which by
construction only runs when the non-blocking acquire at :330 already FAILED --
i.e. exactly when another thread holds the lock and is free to be writing
:332/:336.  bufferedio.c contains zero FT_ATOMIC uses.  `volatile` orders
nothing and is not atomic.

On the GIL build the read and the writes are separated by the GIL, so this is
free-threading-only.  Hammer a contended BufferedReader to make :299 execute
concurrently with :332/:336.

Usage:  python io_buffered_owner_race.py [nthreads] [seconds]
"""

import io
import sys
import threading
import time

NTHREADS = int(sys.argv[1]) if len(sys.argv) > 1 else 8
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0

stop = threading.Event()
errors = []


class SlowRaw(io.RawIOBase):
    """Slow enough that ENTER_BUFFERED's non-blocking acquire keeps failing,
    which is the only way to reach the :299 read."""

    def readable(self):
        return True

    def readinto(self, b):
        # A real detach (time.sleep) is required: on the free-threaded build
        # the clinic @critical_section on _io._Buffered.read is taken BEFORE
        # ENTER_BUFFERED, so without a detach the raw lock never contends and
        # bufferedio.c:299 is unreachable.  _PyThreadState_Detach ->
        # _PyCriticalSection_SuspendAll releases the section, letting a second
        # thread reach ENTER_BUFFERED while this one still holds the raw lock.
        time.sleep(0.004)
        b[0] = 65
        return 1


buf = io.BufferedReader(SlowRaw(), buffer_size=8)


def worker():
    while not stop.is_set():
        try:
            buf.read(1)
        except RuntimeError as e:
            # "reentrant call inside ..." from a bad :299 read would land here
            errors.append(str(e))
        except Exception:
            pass


ts = [threading.Thread(target=worker, daemon=True) for _ in range(NTHREADS)]
for t in ts:
    t.start()
time.sleep(SECONDS)
stop.set()
for t in ts:
    t.join(5)

print(f"threads={NTHREADS} seconds={SECONDS} spurious_reentrant_errors={len(errors)}")
if errors:
    print("sample:", errors[0])

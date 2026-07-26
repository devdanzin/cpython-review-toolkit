"""Re-__init__ of a BufferedReader frees the buffer AND the lock mid-read.

_bufferedreader_fill_buffer (Modules/_io/bufferedio.c:1900) computes
`self->buffer + start` and hands that raw pointer to _bufferedreader_raw_read,
which wraps it in a memoryview whose `obj` is NULL (bufferedio.c:2073, the
comment says so) and passes it to user `raw.readinto()`.

_buffered_init (bufferedio.c:846-858), reachable from Python as
`buf.__init__(...)`, does:

    if (self->buffer) PyMem_Free(self->buffer);      /* :847-848 */
    self->buffer = PyMem_Malloc(self->buffer_size);
    ...
    if (self->lock)   PyThread_free_lock(self->lock); /* :854-855 */
    self->lock = PyThread_allocate_lock();

__init__ takes no lock at all -- it is one of the eight clinic blocks in the
file with no @critical_section -- and ENTER_BUFFERED's owner check lives only
in _enter_buffered_busy, which __init__ never calls.  So calling it from
inside readinto():

  1. frees the block the live memoryview points at  -> heap-UAF WRITE
  2. frees the PyThread_type_lock this frame holds  -> LEAVE_BUFFERED
     (bufferedio.c:334) then releases a *different*, never-acquired lock.

Single-threaded.  No _testcapi.  No revive-by-address.

Usage:  python io_buffered_reinit_frees_lock.py [io|pyio]
"""

import sys

backend = sys.argv[1] if len(sys.argv) > 1 else "io"
if backend == "pyio":
    import _pyio as io
else:
    import io

BUFSIZE = 8192


class Inner(io.RawIOBase):
    def readable(self):
        return True

    def readinto(self, b):
        b[0] = 66
        return 1


class Evil(io.RawIOBase):
    def __init__(self):
        self.armed = True

    def readable(self):
        return True

    def readinto(self, b):
        if self.armed:
            self.armed = False
            # frees self->buffer (which `b` views) and self->lock (held here)
            buf.__init__(Inner(), buffer_size=BUFSIZE)
        # write through the now-dangling memoryview
        for i in range(min(len(b), 4096)):
            b[i] = 67
        return min(len(b), 4096)


buf = io.BufferedReader(Evil(), buffer_size=BUFSIZE)
print(f"{backend}: calling read()", flush=True)
data = buf.read(64)
print(f"{backend}: read() returned {len(data)} bytes: {data[:8]!r}", flush=True)
# LEAVE_BUFFERED has now released the *new* lock, which was never acquired.
# A subsequent operation should therefore find the object lockable when it is
# logically still held -- and a second entry re-runs the whole thing.
print(f"{backend}: second read -> {buf.read(8)!r}", flush=True)
print(f"{backend}: survived", flush=True)

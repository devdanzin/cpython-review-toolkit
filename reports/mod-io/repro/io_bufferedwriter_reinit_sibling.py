"""Sibling of the reader finding: the BufferedWriter path has the same shape.

_bufferedwriter_raw_write (Modules/_io/bufferedio.c:1983-1984) builds the same
obj==NULL memoryview over self->buffer and hands it to user raw.write(), while
_io_BufferedWriter_write_impl holds the buffered lock (ENTER at :2097).
Re-__init__ from inside raw.write() frees that buffer (:847) and that lock
(:854).

Usage: python io_bufferedwriter_reinit_sibling.py [io|pyio]
"""
import sys
backend = sys.argv[1] if len(sys.argv) > 1 else "io"
if backend == "pyio":
    import _pyio as io
else:
    import io

BUFSIZE = 128


class Plain(io.RawIOBase):
    def writable(self):
        return True

    def write(self, b):
        return len(b)


class Evil(io.RawIOBase):
    def __init__(self):
        self.armed = True

    def writable(self):
        return True

    def write(self, b):
        if self.armed:
            self.armed = False
            buf.__init__(Plain(), buffer_size=BUFSIZE)   # frees buffer + lock
        return bytes(b) and len(b)          # reads through the dangling view


buf = io.BufferedWriter(Evil(), buffer_size=BUFSIZE)
print(f"{backend}: writing", flush=True)
buf.write(b"A" * (BUFSIZE - 1))
buf.write(b"B" * (BUFSIZE * 2))     # forces a flush -> raw.write()
print(f"{backend}: survived", flush=True)

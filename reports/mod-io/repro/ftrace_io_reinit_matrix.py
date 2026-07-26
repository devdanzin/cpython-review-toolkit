"""ft-race-scanner: the __init__-guard asymmetry across Modules/_io, measured.

Only 2 of the 9 _io types whose __init__ is exposed to Python declare
@critical_section on it:

    _io.BytesIO.__init__          bytesio.c:1116   @critical_section   <- GUARDED TWIN
    _io.TextIOWrapper.__init__    textio.c:1127    @critical_section   <- GUARDED TWIN
    _io.StringIO.__init__         stringio.c:673   --
    _io.IncrementalNewlineDecoder.__init__ textio.c:232  --
    _io.BufferedReader.__init__   bufferedio.c:1584 --
    _io.BufferedWriter.__init__   bufferedio.c:1932 --
    _io.BufferedRWPair.__init__   bufferedio.c:2256 --
    _io.BufferedRandom.__init__   bufferedio.c:2473 --
    _io.FileIO.__init__           fileio.c:225      --

and none of the unguarded ones takes ENTER_BUFFERED / any other lock either,
while every *other* entry point of the buffered types takes BOTH the clinic
critical section and ENTER_BUFFERED.

The unguarded ones free live heap on re-init:
    _buffered_init:847   PyMem_Free(self->buffer)      (+ :855 PyThread_free_lock(self->lock))
    BufferedReader.__init__:1601  Py_XSETREF(self->raw, ...)   drops the old raw
    StringIO.__init__:723  PyUnicodeWriter_Discard(self->writer); self->writer = NULL
    FileIO.__init__:470    PyMem_Free(self->stat_atopen)

Usage: python ftrace_io_reinit_matrix.py <type> [seconds]
types: bytesio textiowrapper stringio nldecoder bufreader bufwriter bufrandom
       bufrwpair fileio
"""

import io
import os
import sys
import tempfile
import threading

WHICH = sys.argv[1]
DUR = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
NPAIR = 4

stop = threading.Event()
noise = []

_fd, PATH = tempfile.mkstemp()
os.write(_fd, b"line one\nline two\nline three\n" * 300)
os.close(_fd)

BLOB = b"line one\nline two\nline three\n" * 300
TXT = "line one\nline two\nline three\n" * 300


def guard(fn):
    def w():
        while not stop.is_set():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                noise.append(type(e).__name__)
                del noise[32:]

    return w


def build(which):
    if which == "bytesio":
        o = io.BytesIO(BLOB)
        return o, (lambda: o.__init__(BLOB)), (lambda: (o.read(64), o.seek(0), o.getvalue()))
    if which == "textiowrapper":
        o = io.TextIOWrapper(io.BytesIO(BLOB))
        return o, (lambda: o.__init__(io.BytesIO(BLOB))), (lambda: (o.read(64), o.readline()))
    if which == "stringio":
        o = io.StringIO(TXT)
        return o, (lambda: o.__init__(TXT)), (lambda: (o.read(64), o.readline(), o.write("q"), o.getvalue()))
    if which == "nldecoder":
        o = io.IncrementalNewlineDecoder(None, True)
        return o, (lambda: o.__init__(None, True)), (lambda: (o.decode("a\r\nb"), o.newlines, o.reset()))
    if which == "bufreader":
        o = io.BufferedReader(io.BytesIO(BLOB))
        return o, (lambda: o.__init__(io.BytesIO(BLOB))), (lambda: (o.read(64), o.readline(), o.peek(8)))
    if which == "bufwriter":
        o = io.BufferedWriter(io.BytesIO())
        return o, (lambda: o.__init__(io.BytesIO())), (lambda: (o.write(b"p" * 300), o.flush()))
    if which == "bufrandom":
        o = io.BufferedRandom(io.BytesIO(BLOB))
        return o, (lambda: o.__init__(io.BytesIO(BLOB))), (lambda: (o.read(64), o.write(b"q" * 40), o.seek(0)))
    if which == "bufrwpair":
        o = io.BufferedRWPair(io.BytesIO(BLOB), io.BytesIO())
        return o, (lambda: o.__init__(io.BytesIO(BLOB), io.BytesIO())), (lambda: (o.read(64), o.write(b"q" * 40)))
    if which == "fileio":
        o = io.FileIO(PATH, "rb")
        return o, (lambda: o.__init__(PATH, "rb")), (lambda: (o.seek(0), o.read(64), o.fileno()))
    raise SystemExit("unknown " + which)


obj, reinit, user = build(WHICH)
ts = []
for _ in range(NPAIR):
    ts.append(threading.Thread(target=guard(reinit), daemon=True))
    ts.append(threading.Thread(target=guard(user), daemon=True))
for t in ts:
    t.start()
stop.wait(DUR)
stop.set()
for t in ts:
    t.join(5.0)
try:
    os.unlink(PATH)
except OSError:
    pass
print("survived %s; noise=%s" % (WHICH, sorted(set(noise))))

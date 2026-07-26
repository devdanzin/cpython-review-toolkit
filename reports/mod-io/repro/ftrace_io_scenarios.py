"""ft-race-scanner: multi-scenario _io stress driver (one scenario per process).

Usage: python ftrace_io_scenarios.py <scenario> [seconds]

scenarios
  stringio_iternext  stringio.c:410 stringio_iternext takes NO critical section while
                     every _io.StringIO clinic impl is @critical_section (TSAN-0007 /
                     gh-153296; the earlier fix e6c3039cb39 was REVERTED by 73431356d32)
  buffered_iternext  bufferedio.c:1504 CHECK_INITIALIZED reads self->ok BEFORE the
                     critical section opened at :1512, racing detach()'s self->ok=0
                     (TSAN-0032, residual of PR #150295)
  fileio_fd          fileio.c: __init__ / close / fileno on one shared FileIO
                     (self->fd plain int, self->stat_atopen PyMem block) -- gh-151707
  bytesio_exports    getbuffer/release (exports counter) vs truncate/write/seek resize
  buffered_owner     bufferedio.c:299 _enter_buffered_busy reads self->owner BEFORE
                     acquiring self->lock, racing LEAVE_BUFFERED's self->owner = 0
  bufferedwriter     BufferedWriter.__init__ racing write()/flush()
"""

import io
import os
import sys
import tempfile
import threading

SCEN = sys.argv[1]
DUR = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
NPAIR = 4

stop = threading.Event()
noise = []


def guard(fn):
    def wrapped(*a):
        while not stop.is_set():
            try:
                fn(*a)
            except Exception as e:  # noqa: BLE001
                noise.append(type(e).__name__)
                del noise[64:]

    return wrapped


# ---------------------------------------------------------------- stringio
def s_stringio_iternext():
    obj = io.StringIO("alpha\nbeta\ngamma\ndelta\nepsilon\n" * 200)

    @guard
    def it():
        next(iter(obj), None)
        next(obj, None)

    @guard
    def mutator():
        obj.seek(0)
        obj.write("zzz\n")
        obj.truncate(10)
        obj.seek(0)

    return obj, [it, mutator]


# ---------------------------------------------------------------- buffered iternext
def s_buffered_iternext():
    obj = io.BufferedReader(io.BytesIO(b"alpha\nbeta\ngamma\n" * 400))

    @guard
    def it():
        next(obj, None)

    @guard
    def detacher():
        try:
            obj.detach()
        except ValueError:
            obj.__init__(io.BytesIO(b"alpha\nbeta\n" * 400))

    return obj, [it, detacher]


# ---------------------------------------------------------------- fileio
def s_fileio():
    fd, path = tempfile.mkstemp()
    os.write(fd, b"x" * 8192)
    os.close(fd)
    obj = io.FileIO(path, "rb")

    @guard
    def reinit():
        obj.__init__(path, "rb")

    @guard
    def user():
        obj.fileno()
        obj.read(16)
        obj.seek(0)

    @guard
    def closer():
        obj.close()

    return obj, [reinit, user, closer]


# ---------------------------------------------------------------- bytesio exports
def s_bytesio_exports():
    obj = io.BytesIO(b"y" * 4096)

    @guard
    def exporter():
        mv = obj.getbuffer()
        mv[0:1] = b"z"
        mv.release()

    @guard
    def resizer():
        obj.seek(0)
        obj.write(b"q" * 128)
        obj.truncate(2048)
        obj.truncate(4096)
        obj.getvalue()

    return obj, [exporter, resizer]


# ---------------------------------------------------------------- buffered owner
def s_buffered_owner():
    obj = io.BufferedReader(io.BytesIO(b"m" * 65536), buffer_size=64)

    @guard
    def reader():
        obj.seek(0)
        obj.read(4096)
        obj.peek(8)

    return obj, [reader]


# ---------------------------------------------------------------- buffered writer
def s_bufferedwriter():
    obj = io.BufferedWriter(io.BytesIO(), buffer_size=1024)

    @guard
    def reinit():
        obj.__init__(io.BytesIO(), buffer_size=1024)

    @guard
    def writer():
        obj.write(b"p" * 300)
        obj.flush()

    return obj, [reinit, writer]


TABLE = {
    "stringio_iternext": s_stringio_iternext,
    "buffered_iternext": s_buffered_iternext,
    "fileio_fd": s_fileio,
    "bytesio_exports": s_bytesio_exports,
    "buffered_owner": s_buffered_owner,
    "bufferedwriter": s_bufferedwriter,
}

obj, fns = TABLE[SCEN]()
ts = []
for i in range(NPAIR * len(fns)):
    ts.append(threading.Thread(target=fns[i % len(fns)], daemon=True))
for t in ts:
    t.start()
stop.wait(DUR)
stop.set()
for t in ts:
    t.join(5.0)
print("done %s; noise=%s" % (SCEN, sorted(set(noise))))

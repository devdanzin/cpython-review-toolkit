"""ft-race-scanner repro C: FileIO.__init__ hands a PyMem block to fstat()
with the GIL RELEASED, and close() frees that block.

Modules/_io/fileio.c
  _io_FileIO___init___impl:470  PyMem_Free(self->stat_atopen);
                          :471  self->stat_atopen = PyMem_New(struct _Py_stat_struct, 1);
                          :476  Py_BEGIN_ALLOW_THREADS            <-- GIL dropped
                          :477  fstat_result = _Py_fstat_noraise(self->fd, self->stat_atopen);
                          :478  Py_END_ALLOW_THREADS
  internal_close:138            PyMem_Free(self->stat_atopen);
                :139            self->stat_atopen = NULL;

Modules/_io/fileio.c contains ZERO critical sections (0 `@critical_section`
directives, 0 Py_BEGIN_CRITICAL_SECTION in clinic/fileio.c.h), so nothing
serialises the two.  Because the window is opened by Py_BEGIN_ALLOW_THREADS,
this is reachable on the DEFAULT GIL build, not only under free-threading.

Run on a GIL ASan build for heap evidence (FT ASan has no shadow for the
object heap -- but this block is PyMem_Malloc'd, so check both).
"""

import io
import os
import sys
import tempfile
import threading

DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
NPAIR = int(sys.argv[2]) if len(sys.argv) > 2 else 4

fd, PATH = tempfile.mkstemp()
os.write(fd, b"z" * 65536)
os.close(fd)

f = io.FileIO(PATH, "rb")
stop = threading.Event()
noise = []


def reinit():
    while not stop.is_set():
        try:
            f.__init__(PATH, "rb")
        except Exception as e:  # noqa: BLE001
            noise.append(type(e).__name__)
            del noise[32:]


def closer():
    while not stop.is_set():
        try:
            f.close()
        except Exception as e:  # noqa: BLE001
            noise.append(type(e).__name__)
            del noise[32:]


def statreader():
    # readall()/isatty() read through self->stat_atopen (fileio.c:765, :1250, :1306)
    while not stop.is_set():
        try:
            f.seek(0)
            f.readall()
            f.isatty()
        except Exception as e:  # noqa: BLE001
            noise.append(type(e).__name__)
            del noise[32:]


ts = []
for _ in range(NPAIR):
    ts.append(threading.Thread(target=reinit, daemon=True))
    ts.append(threading.Thread(target=closer, daemon=True))
    ts.append(threading.Thread(target=statreader, daemon=True))
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
print("survived; noise=%s" % sorted(set(noise)))

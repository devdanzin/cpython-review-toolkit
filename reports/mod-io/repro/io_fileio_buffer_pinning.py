"""(e) Can the buffer handed to fileio's GIL-released read()/write() be invalidated?

fileio.c does its raw I/O through _Py_read / _Py_write (fileio.c:696, :833,
:898, :940), which release the GIL around the syscall.  The destination is
`buffer->buf` from a `Py_buffer(accept={rwbuffer})` clinic argument, i.e. a
PyObject_GetBuffer(PyBUF_WRITABLE) held for the duration of the call.

This script parks a reader thread inside a blocking read() from a pipe with a
live writable export, then tries every stdlib mutation that could realloc or
free the exporter's storage.  Each one must raise BufferError; anything that
succeeds writes into freed/moved memory once the pipe delivers.

Usage:  python io_fileio_buffer_pinning.py
Exit 0 = every mutation was refused (buffer correctly pinned).
Exit 9 = at least one mutation was allowed while the export was live.
"""

import io
import os
import sys
import threading
import time

r, w = os.pipe()
raw = io.FileIO(r, "rb", closefd=False)

target = bytearray(64)
mv = memoryview(target)

bio = io.BytesIO(b"\0" * 64)
bio_view = bio.getbuffer()

in_read = threading.Event()
results = []


def reader():
    in_read.set()
    n = raw.readinto(mv)          # blocks in _Py_read with the GIL released
    results.append(n)


t = threading.Thread(target=reader, daemon=True)
t.start()
in_read.wait(5)
time.sleep(0.7)                   # ensure the thread is inside read(2)

failures = []


def attempt(label, fn):
    try:
        fn()
    except BufferError as e:
        print(f"  refused   {label}: BufferError: {e}")
    except Exception as e:
        print(f"  other     {label}: {type(e).__name__}: {e}")
    else:
        print(f"  ALLOWED   {label}  <-- buffer not pinned")
        failures.append(label)


print("mutations attempted while a writable export is live in a blocking read:")
attempt("bytearray.clear()", target.clear)
attempt("bytearray += 4096 bytes", lambda: target.extend(b"x" * 4096))
attempt("bytearray[:] = b''", lambda: target.__setitem__(slice(None), b""))
attempt("del bytearray[0:32]", lambda: target.__delitem__(slice(0, 32)))
attempt("bytearray.pop()", target.pop)
attempt("BytesIO.close() with live getbuffer()",
        lambda: bio.close())
attempt("BytesIO.truncate(0) with live getbuffer()",
        lambda: bio.truncate(0))
attempt("BytesIO.write() growing past the export",
        lambda: bio.write(b"y" * 8192))

os.write(w, b"Z" * 32)
t.join(5)
print(f"readinto returned: {results}")
print(f"bytearray now: len={len(target)} first8={bytes(target[:8])!r}")

if failures:
    print(f"UNPINNED: {failures}")
    sys.exit(9)
print("all mutations refused -- buffer correctly pinned")
sys.exit(0)

"""bytesio.c: bytesiobuf_getbuffer has no closed check -> NULL deref after close().

Modules/_io/bytesio.c:1274 bytesiobuf_getbuffer_lock_held
    bytesio *b = bytesio_CAST(obj->source);
    if (FT_ATOMIC_LOAD_SSIZE_RELAXED(b->exports) == 0 && SHARED_BUF(b)) { ... }
        -> SHARED_BUF(b) == !_PyObject_IsUniquelyReferenced(b->buf)
        -> Py_REFCNT(NULL) when the BytesIO has been closed
    (void)PyBuffer_FillInfo(view, op, PyBytes_AS_STRING(b->buf), b->string_size, ...)

_io.BytesIO.getbuffer() checks CHECK_CLOSED before it builds the intermediate
_io._BytesIOBuffer, but that intermediate is reachable from Python as
memoryview.obj, and re-exporting it goes straight to bf_getbuffer with no
closed check of its own.

Guarded twin: every other public entry point in bytesio.c opens with
CHECK_CLOSED / check_closed (bytesio.c:46, and :325 :340 :355 :370 :393 :415 ...).

Usage: <python> bytesio_buf_after_close.py [mode]
  mode=probe    (default) close then re-export -> expect SIGSEGV
  mode=noclose  control: same dance without close() -> must be clean
"""
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "probe"

import io

bio = io.BytesIO(b"hello world" * 100)
m = bio.getbuffer()
inner = m.obj                      # the _io._BytesIOBuffer intermediate
print("inner type:", type(inner).__name__)
m.release()                        # exports 1 -> 0, so close() is now allowed

if MODE == "probe":
    bio.close()                    # self->buf = NULL
    print("closed; bio.closed =", bio.closed)

sys.stdout.flush()
m2 = memoryview(inner)             # -> bytesiobuf_getbuffer on a closed BytesIO
print("re-export SURVIVED, len =", len(m2), "bytes =", bytes(m2)[:16])
m2.release()

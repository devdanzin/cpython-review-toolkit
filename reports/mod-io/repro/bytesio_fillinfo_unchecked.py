"""bytesio.c:1288 discards PyBuffer_FillInfo()'s failure with a (void) cast.

Modules/_io/bytesio.c:1274 bytesiobuf_getbuffer_lock_held
    /* cannot fail if view != NULL and readonly == 0 */
    (void)PyBuffer_FillInfo(view, op,
                            PyBytes_AS_STRING(b->buf), b->string_size,
                            0, flags);
    FT_ATOMIC_ADD_SSIZE(b->exports, 1);
    return 0;

The comment is wrong.  Objects/abstract.c:774 PyBuffer_FillInfo has a THIRD
failure mode independent of view/readonly:

    if (flags != PyBUF_SIMPLE) {          /* fast path */
        if (flags == PyBUF_READ || flags == PyBUF_WRITE) {
            PyErr_BadInternalCall();
            return -1;
        }

PyBUF_READ is 0x100 and PyBUF_WRITE is 0x200 (Include/pybuffer.h:137-138).
PEP 688 exposes bf_getbuffer to Python as __buffer__(flags), and
typeobject.c:10345 wrap_buffer forwards ANY int in [INT_MIN, INT_MAX]
straight through.  So `inner.__buffer__(0x100)` makes FillInfo bail with the
Py_buffer completely unwritten, while bytesiobuf_getbuffer_lock_held
increments exports and returns 0 (success) with an exception set.

Guarded twin: Objects/bytearrayobject.c:66 bytearray_getbuffer_lock_held --
same FillInfo(.., 0, flags) call, but

    if (PyBuffer_FillInfo(view, (PyObject*)obj, ptr, Py_SIZE(obj), 0, flags) < 0) {
        return -1;
    }
    obj->ob_exports++;

Usage: <python> bytesio_fillinfo_unchecked.py [flags_int]
"""
import sys

FLAGS = int(sys.argv[1], 0) if len(sys.argv) > 1 else 0x100  # PyBUF_READ

import io

bio = io.BytesIO(b"payload" * 64)
m = bio.getbuffer()
inner = m.obj                      # _io._BytesIOBuffer, exports == 1
m.release()                        # exports -> 0
print("exports back to 0 (truncate works):", end=" ")
try:
    bio.truncate(len(bio.getvalue()))
    print("yes")
except BufferError as e:
    print("no --", e)

sys.stdout.flush()
try:
    got = inner.__buffer__(FLAGS)
    print("__buffer__(%#x) RETURNED %r" % (FLAGS, type(got).__name__))
    sys.stdout.flush()
    print("  nbytes =", got.nbytes, " ndim =", got.ndim, " obj =", got.obj)
except BaseException as e:
    print("__buffer__(%#x) raised %s: %s" % (FLAGS, type(e).__name__, e))

sys.stdout.flush()
# Did the failed export still bump the counter?
try:
    bio.truncate(0)
    print("POST: truncate(0) succeeded -> exports still 0")
except BufferError as e:
    print("POST: truncate(0) -> BufferError (export count leaked):", e)
try:
    bio.close()
    print("POST: close() succeeded")
except BufferError as e:
    print("POST: close() -> BufferError (export count leaked):", e)

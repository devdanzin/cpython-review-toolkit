"""FIX candidate: _io.TextIOWrapper.seek() calls decode() on a NULL decoder.

Modules/_io/textio.c:2737 guards the decoder-setstate call:

    if (self->decoder) {
        if (_textiowrapper_decoder_setstate(self, &cookie) < 0)   /* :2740 */
            goto fail;
    }

...but 33 lines later, inside a DIFFERENT `if`, the same field is used with
no guard at all:

    if (cookie.chars_to_skip) {                                   /* :2743 */
        ...
        decoded = PyObject_CallMethodObjArgs(self->decoder,        /* :2773 */
                                             &_Py_ID(decode), ...);

self->decoder stays NULL for the whole lifetime of the wrapper whenever the
underlying buffer answered readable() == False at construction time
(_textiowrapper_set_decoder, textio.c:963: `if (r != 1) return 0;` -- it
returns success WITHOUT creating a decoder).  cookie.chars_to_skip comes
straight out of the integer the caller passes to seek().

The guarded twin is _io_TextIOWrapper_tell_impl, textio.c:2857:
    if (self->decoder == NULL || self->snapshot == NULL) { ... return posobj; }
"""

import io
import os
import struct
import sys

if os.environ.get("PYIO"):
    import _pyio as io


class LyingBuffer:
    """A buffer that reports readable() == False but implements read()."""

    closed = False

    def __init__(self):
        self._pos = 0

    def readable(self):
        return False          # <- makes TextIOWrapper skip decoder creation

    def writable(self):
        return True

    def seekable(self):
        return True

    def read(self, n=-1):
        return b"abcd"[:n] if n >= 0 else b"abcd"

    def read1(self, n=-1):
        return self.read(n)

    def write(self, b):
        return len(b)

    def flush(self):
        return None

    def seek(self, pos, whence=0):
        self._pos = pos
        return pos

    def tell(self):
        return self._pos

    def close(self):
        self.closed = True


def build_cookie(start_pos=0, dec_flags=0, bytes_to_feed=4, chars_to_skip=1,
                 need_eof=0):
    """Mirror textiowrapper_build_cookie() for little-endian LP64."""
    off_t = struct.calcsize("q")           # sizeof(Py_off_t)
    isz = struct.calcsize("i")             # sizeof(int)
    buf = bytearray(off_t + 3 * isz + 1)
    buf[0:off_t] = struct.pack("<q", start_pos)
    buf[off_t:off_t + isz] = struct.pack("<i", dec_flags)
    buf[off_t + isz:off_t + 2 * isz] = struct.pack("<i", bytes_to_feed)
    buf[off_t + 2 * isz:off_t + 3 * isz] = struct.pack("<i", chars_to_skip)
    buf[off_t + 3 * isz] = need_eof
    return int.from_bytes(bytes(buf), "little")


def sc_seek_null_decoder():
    """textio.c:2773 -- seek(cookie) with chars_to_skip != 0 on a wrapper
    whose decoder was never created."""
    t = io.TextIOWrapper(LyingBuffer(), encoding="utf-8")
    print("decoder is None ->", t._CHUNK_SIZE is not None, file=sys.stderr)
    return t.seek(build_cookie(chars_to_skip=1))


def sc_seek_zero_skip():
    """Control: same object, chars_to_skip == 0 -> must NOT crash."""
    t = io.TextIOWrapper(LyingBuffer(), encoding="utf-8")
    return t.seek(build_cookie(chars_to_skip=0))


def sc_tell_null_decoder():
    """Control: tell() has the guarded twin at textio.c:2857 -- must not
    crash on the same object."""
    t = io.TextIOWrapper(LyingBuffer(), encoding="utf-8")
    return t.tell()


def sc_seek_honest_buffer():
    """Control: an honest readable() == True buffer builds a decoder, so the
    same cookie is harmless."""

    class Honest(LyingBuffer):
        def readable(self):
            return True

    t = io.TextIOWrapper(Honest(), encoding="utf-8")
    return t.seek(build_cookie(chars_to_skip=1))


def sc_seek_after_failed_reconfigure():
    """Second, much more reachable route to the same textio.c:2775 sink:
    _textiowrapper_set_decoder (textio.c:976-978) does Py_CLEAR(self->decoder)
    and leaves it NULL when _PyCodecInfo_GetIncrementalDecoder fails, so a
    failed reconfigure() to a codec with no incremental decoder leaves a
    fully-readable wrapper with decoder == NULL and ok == 1."""
    import codecs

    def probe(name):
        if name != "nodecoder":
            return None
        return codecs.CodecInfo(
            name="nodecoder",
            encode=codecs.lookup("utf-8").encode,
            decode=codecs.lookup("utf-8").decode,
            incrementalencoder=codecs.lookup("utf-8").incrementalencoder,
            incrementaldecoder=None,          # <- makes set_decoder fail
        )

    codecs.register(probe)
    t = io.TextIOWrapper(io.BytesIO(b"abcdefgh"), encoding="utf-8")
    try:
        t.reconfigure(encoding="nodecoder")
    except Exception as e:
        sys.stderr.write("  reconfigure raised %r (decoder now NULL)\n" % (e,))
    return t.seek(build_cookie(chars_to_skip=1))




SCENARIOS = {
    name[3:]: fn for name, fn in sorted(globals().items()) if name.startswith("sc_")
}

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--list"
    if arg == "--list":
        for k, fn in SCENARIOS.items():
            print(k, "--", (fn.__doc__ or "").split("\n")[0])
        sys.exit(0)
    sys.stderr.write("RUN %s\n" % arg)
    sys.stderr.flush()
    try:
        out = SCENARIOS[arg]()
        sys.stderr.write("SURVIVED -> %r\n" % (out,))
    except BaseException as exc:
        sys.stderr.write("RAISED %s: %s\n" % (type(exc).__name__, exc))

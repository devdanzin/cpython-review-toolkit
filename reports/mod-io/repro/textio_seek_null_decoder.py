"""textio.c:2775 -- seek() calls decode() on self->decoder without re-checking it for NULL.

:2739 guards the setstate call with `if (self->decoder)`. Thirty-six lines later the
`if (cookie.chars_to_skip)` block at :2744 uses the same field unguarded, reaching
:2775:

    decoded = PyObject_CallMethodObjArgs(self->decoder, &_Py_ID(decode), ...)

self->decoder is NULL for a TextIOWrapper over a non-readable raw stream, and
chars_to_skip comes straight out of the caller's cookie integer.

Little-endian cookie layout (textio.c:2488-2492):
    off 0                       start_pos      Py_off_t   (8)
    off 8                       dec_flags      int        (4)
    off 12                      bytes_to_feed  int        (4)
    off 16                      chars_to_skip  int        (4)
    off 20                      need_eof       char       (1)

Usage:  python textio_seek_null_decoder.py [--pyio]
"""

import sys

if "--pyio" in sys.argv:
    import _pyio as io
else:
    import io


def make_cookie(start_pos=0, dec_flags=0, bytes_to_feed=0, chars_to_skip=0, need_eof=0):
    buf = bytearray(21)
    buf[0:8] = start_pos.to_bytes(8, "little", signed=True)
    buf[8:12] = dec_flags.to_bytes(4, "little", signed=True)
    buf[12:16] = bytes_to_feed.to_bytes(4, "little", signed=True)
    buf[16:20] = chars_to_skip.to_bytes(4, "little", signed=True)
    buf[20] = need_eof
    return int.from_bytes(buf, "little")


class WriteOnlyRaw(io.RawIOBase):
    """Reports NOT readable during __init__, then readable afterwards.

    _textiowrapper_set_decoder runs only when the stream is readable at
    construction time, so self->decoder stays NULL -- but the seek path reads an
    input chunk from the buffer directly, which needs the read to work. A raw
    that lies once satisfies both preconditions.
    """

    _asked = 0
    _data = b"abcdefgh"

    def readable(self):
        type(self)._asked += 1
        return type(self)._asked > 1        # False the first time only

    def read(self, n=-1):
        return self._data[:n if n and n > 0 else len(self._data)]

    def readinto(self, b):
        d = self._data[:len(b)]
        b[:len(d)] = d
        return len(d)

    def writable(self):
        return True

    def seekable(self):
        return True

    def write(self, b):
        return len(b)

    def seek(self, pos, whence=0):
        return 0

    def tell(self):
        return 0


def main():
    t = io.TextIOWrapper(WriteOnlyRaw(), encoding="utf-8")
    print("readable=%r  (decoder should be NULL on the C side)" % t.readable(),
          file=sys.stderr)
    cookie = make_cookie(start_pos=0, chars_to_skip=1, bytes_to_feed=4)
    print("seeking with chars_to_skip=1 -> enters the :2744 block", file=sys.stderr)
    try:
        t.seek(cookie)
    except BaseException as exc:
        print("seek -> %s: %s" % (type(exc).__name__, str(exc)[:120]), file=sys.stderr)
    else:
        print("seek -> returned normally", file=sys.stderr)
    print("survived", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

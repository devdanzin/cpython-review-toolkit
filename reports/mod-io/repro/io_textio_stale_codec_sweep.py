"""Sweep: `self->encoder` / `self->decoder` / `self->encodefunc` reads in
Modules/_io/textio.c that happen AFTER a call into arbitrary user Python.

gh-143008 (commit db4b1948bc4) hardened textio.c against exactly this shape, but
only for `self->buffer` -- every access to it now goes through
buffer_access_safe().  `self->encoder` and `self->decoder` are equally
user-nullable (a re-entrant TextIOWrapper.__init__ Py_CLEARs both at
textio.c:1215-1216 and the `error:` label at :1327 leaves them NULL) and did not
get an accessor.

usage:  python io_textio_stale_codec_sweep.py --case <name> [--pyio]
        python io_textio_stale_codec_sweep.py --list
"""

import codecs
import sys

PYIO = "--pyio" in sys.argv
if PYIO:
    import _pyio as io
else:
    import io

BAD = "no-such-codec-zzz"


def wreck(t):
    """Re-enter TextIOWrapper.__init__ with a bogus encoding.

    textio.c:1215-1216 Py_CLEARs self->encoder and self->decoder before the
    codec lookup at :1257; the lookup fails, `goto error` at :1260 returns -1
    and both stay NULL.
    """
    try:
        t.__init__(io.BytesIO(), encoding=BAD)
    except BaseException as e:
        print("  [re-init raised %s]" % type(e).__name__, file=sys.stderr)


# ------------------------------------------------------------- helper codec

class HostileIncrementalDecoder(codecs.IncrementalDecoder):
    """An incremental decoder whose getstate() re-enters __init__ once."""
    target = None

    def __init__(self, errors="strict"):
        self.errors = errors
        self.buf = b""

    def decode(self, input, final=False):
        return bytes(input).decode("latin-1")

    def getstate(self):
        t, HostileIncrementalDecoder.target = (HostileIncrementalDecoder.target,
                                               None)
        if t is not None:
            print("  [decoder.getstate re-entering __init__]", file=sys.stderr)
            wreck(t)
        return (self.buf, 0)

    def setstate(self, state):
        self.buf = state[0]

    def reset(self):
        self.buf = b""


class HostileIncrementalEncoder(codecs.IncrementalEncoder):
    def __init__(self, errors="strict"):
        self.errors = errors

    def encode(self, input, final=False):
        return input.encode("latin-1")

    def reset(self):
        pass

    def getstate(self):
        return 0

    def setstate(self, state):
        pass


def _hostile_search(name):
    if name != "hostile":
        return None
    return codecs.CodecInfo(
        name="hostile",
        encode=lambda s, errors="strict": (s.encode("latin-1"), len(s)),
        decode=lambda b, errors="strict": (bytes(b).decode("latin-1"), len(b)),
        incrementalencoder=HostileIncrementalEncoder,
        incrementaldecoder=HostileIncrementalDecoder,
    )


codecs.register(_hostile_search)


class HostileStr(str):
    """A str whose .replace() re-enters __init__ once."""
    target = None

    def replace(self, *a, **kw):
        t, HostileStr.target = HostileStr.target, None
        if t is not None:
            print("  [str.replace re-entering __init__]", file=sys.stderr)
            wreck(t)
        return str.replace(self, *a, **kw)


# ---------------------------------------------------------------------- cases

def case_write_encoder_cleared():
    """textio.c:1783 -- write() NULL-checks self->encoder at :1741, then runs
    a user str.replace() at :1754, then uses self->encoder unchecked."""
    t = io.TextIOWrapper(io.BytesIO(), encoding="latin-1", newline="\r\n")
    HostileStr.target = t
    return t.write(HostileStr("a\nb"))


def case_tell_decoder_cleared():
    """textio.c:2896/2926 -- tell() NULL-checks self->decoder at :2857, runs a
    user decoder.getstate() at :2889, then uses self->decoder unchecked."""
    buf = io.BytesIO(b"abcdefgh" * 4)
    t = io.TextIOWrapper(buf, encoding="hostile", newline="")
    t.read(5)                     # advance decoded_chars_used past 0
    HostileIncrementalDecoder.target = t
    return t.tell()


def case_seek_decoder_cleared():
    """textio.c:2775 -- seek() checks self->decoder at :2739, then reaches
    decode() at :2775 after intervening user code."""
    buf = io.BytesIO(b"abcdefgh" * 4)
    t = io.TextIOWrapper(buf, encoding="hostile", newline="")
    t.read(5)
    cookie = t.tell()
    HostileIncrementalDecoder.target = t
    return t.seek(cookie)


def case_reconfigure_decoder_cleared():
    """textio.c:1501 -- reconfigure() drives _PyFile_Flush at :1492 then
    set_newline/change_encoding."""
    t = io.TextIOWrapper(io.BytesIO(b"abc"), encoding="hostile", newline="")
    HostileIncrementalDecoder.target = t
    return t.reconfigure(newline="\n")


CASES = {k[5:]: v for k, v in sorted(globals().items()) if k.startswith("case_")}


def main():
    if "--list" in sys.argv:
        for name in CASES:
            print(name)
        return
    i = sys.argv.index("--case")
    name = sys.argv[i + 1]
    print("case=%s backend=%s" % (name, "_pyio" if PYIO else "_io"), file=sys.stderr)
    try:
        out = CASES[name]()
        print("RESULT: returned %r" % (out,), file=sys.stderr)
    except BaseException as exc:
        print("RESULT: raised %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
    print("survived", file=sys.stderr)


if __name__ == "__main__":
    main()

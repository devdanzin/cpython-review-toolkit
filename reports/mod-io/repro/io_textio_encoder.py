"""textio.c:1783 -- self->encoder read after user Python ran.

_io_TextIOWrapper_write_impl guards at :1741 (self->encoder == NULL), then at
:1754 calls text.replace() -- a str SUBCLASS can override replace() -- and only
at :1783 does PyObject_CallMethodOneArg(self->encoder, "encode", text).

_textiowrapper_set_encoder (:1032-1036) does Py_CLEAR(self->encoder) and leaves
it NULL when _PyCodecInfo_GetIncrementalEncoder fails, so a reconfigure() to a
codec with no incremental encoder NULLs it inside that window.

Usage: io_textio_encoder.py <backend>
"""

import codecs
import sys

backend = sys.argv[1] if len(sys.argv) > 1 else "io"
if backend == "_pyio":
    import _pyio as iomod
else:
    import io as iomod

state = {}

# A codec that has a decoder but NO incremental encoder -> set_encoder fails
# and leaves self->encoder == NULL.
_utf8 = codecs.lookup("utf-8")


def _search(name):
    if name != "noincenc":
        return None
    return codecs.CodecInfo(
        name="noincenc",
        encode=_utf8.encode,
        decode=_utf8.decode,
        incrementalencoder=None,  # <- the hole
        incrementaldecoder=_utf8.incrementaldecoder,
        streamreader=_utf8.streamreader,
        streamwriter=_utf8.streamwriter,
    )


codecs.register(_search)


class EvilStr(str):
    fired = False

    def replace(self, *a, **k):
        cls = type(self)
        if not cls.fired:
            cls.fired = True
            try:
                state["f"].reconfigure(encoding="noincenc")
            except Exception as e:
                print("reconfigure raised:", type(e).__name__, e, flush=True)
        return str(self)


# write_through/newline settings chosen so writetranslate + writenl are live,
# which is what routes us through text.replace() at :1754.
f = iomod.TextIOWrapper(iomod.BytesIO(), encoding="utf-16", newline="\r\n")
state["f"] = f

print("before write", flush=True)
f.write(EvilStr("a\nb"))
print("survived", flush=True)

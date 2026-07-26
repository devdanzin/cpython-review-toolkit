"""textio.c:350 -- _PyIncrementalNewlineDecoder_decode reads and writes its own
`self` after the nested user decoder call has freed it.

Modules/_io/textio.c:327
    _PyIncrementalNewlineDecoder_decode(PyObject *myself, PyObject *input, int final)
    {
        nldecoder_object *self = nldecoder_object_CAST(myself);
        ...
        if (self->decoder != Py_None) {
            output = PyObject_CallMethodObjArgs(self->decoder,          /* :339 -- USER PYTHON */
                &_Py_ID(decode), input, final ? Py_True : Py_False, NULL);
        }
        ...
        output_len = PyUnicode_GET_LENGTH(output);
        if (self->pendingcr && (final || output_len > 0)) {             /* :350 -- UAF READ  */
            ...
            self->pendingcr = 0;                                        /* :365 -- UAF WRITE */
        }
        ...
        self->seennl |= seennl;                                         /* :511 -- UAF WRITE */

`myself` arrives BORROWED from the two textio call sites:

    textio.c:1001  chars = _PyIncrementalNewlineDecoder_decode(decoder, bytes, eof);
                   /* decoder == self->decoder, passed borrowed through
                      _textiowrapper_decode(state, self->decoder, ...) at :2012 */
    textio.c:2093  decoded = _PyIncrementalNewlineDecoder_decode(self->decoder, ...)

and `self->decoder` is dropped by TextIOWrapper.reconfigure():

    textio.c:976   Py_CLEAR(self->decoder);            in _textiowrapper_set_decoder
    textio.c:1402                                      from textiowrapper_change_encoding
    textio.c:1501                                      from _io_TextIOWrapper_reconfigure_impl

so a user codec's decode() that calls f.reconfigure(...) frees the
nldecoder_object out from under the C frame that is standing in it.

Guarded twins, both in the same file:
  * textio.c:1003 -- the sibling branch of the very same `if`:
        chars = PyObject_CallMethodObjArgs(decoder, &_Py_ID(decode), ...)
    never dereferences `decoder` after the call, so the generic (non-newline)
    decoder path is immune.  Only the C fast path at :1001 is exposed.
  * textio.c:533 _io_IncrementalNewlineDecoder_decode_impl reaches the same
    function with `self` pinned by the bound-method call machinery.
  * stringio.c:199 passes self->decoder borrowed too, but is safe BY
    CONSTRUCTION: StringIO always builds its IncrementalNewlineDecoder with an
    inner decoder of Py_None (stringio.c:754-757), so textio.c:338 takes the
    `output = Py_NewRef(input)` branch and no user Python ever runs.

Usage: <python> textio_nldecoder_self_uaf.py [mode]
  mode=probe   (default) inner decoder calls f.reconfigure() -> UAF
  mode=control identical codec, no reconfigure -> must be clean
"""
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "probe"

import codecs
import io

state = {"f": None, "fired": 0, "junk": None}


class ReDecoder:
    def __init__(self, errors="strict"):
        self.errors = errors

    def decode(self, data, final=False):
        # We are called from textio.c:339, i.e. from inside
        # _PyIncrementalNewlineDecoder_decode, whose `self` is the
        # _io.IncrementalNewlineDecoder wrapping *us*.
        if MODE == "probe" and not state["fired"] and state["f"] is not None:
            state["fired"] = 1
            # -> _textiowrapper_set_decoder -> Py_CLEAR(self->decoder)
            #    -> incrementalnewlinedecoder_dealloc (textio.c:296) -> free()
            state["f"].reconfigure(newline="\r")
            # recycle the freed nldecoder_object block
            state["junk"] = [bytearray(64) for _ in range(8000)]
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
    if name != "uaf_test_codec":
        return None
    return codecs.CodecInfo(
        name="uaf_test_codec",
        encode=lambda s, errors="strict": (s.encode("latin-1"), len(s)),
        decode=lambda b, errors="strict": (bytes(b).decode("latin-1"), len(b)),
        incrementalencoder=ReEncoder,
        incrementaldecoder=ReDecoder,
    )


codecs.register(search)

# newline=None (the default) makes readuniversal true, which is what wraps the
# user decoder in an _io.IncrementalNewlineDecoder (textio.c:981-988).
raw = io.BufferedReader(io.BytesIO(b"abcdefghijklmnopqrstuvwxyz" * 64))
f = io.TextIOWrapper(raw, encoding="uaf_test_codec")
state["f"] = f

print("reading...")
sys.stdout.flush()
try:
    data = f.read(5)
    print("read ->", repr(data))
except BaseException as e:
    print("read raised %s: %s" % (type(e).__name__, e))
sys.stdout.flush()
print("fired =", state["fired"])
print("survived")

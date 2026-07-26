"""Leak measurement for the mod-io CONSIDER findings, using weakrefs not gc counts.

gc.get_objects() length is too noisy to call a leak. Each case here holds a weakref
to the object that re-initialization is supposed to discard and reports whether it
was actually finalized.

    python verify_leaks.py --case <name> [--pyio]
"""

import gc
import sys
import weakref

if "--pyio" in sys.argv:
    import _pyio as io
else:
    import io

DEBUG = hasattr(sys, "gettotalrefcount")


def _refs():
    return sys.gettotalrefcount() if DEBUG else -1


def case_rwpair_reinit():
    """BufferedRWPair.__init__ twice: is the first reader/writer pair released?"""
    r1, w1 = io.BytesIO(b"a" * 16), io.BytesIO()
    p = io.BufferedRWPair(r1, w1)
    # The Buffered wrappers the pair built around r1/w1 are what should die.
    wr = weakref.ref(r1)
    ww = weakref.ref(w1)
    del r1, w1
    gc.collect()
    print("after construction: reader alive=%r writer alive=%r"
          % (wr() is not None, ww() is not None), file=sys.stderr)

    p.__init__(io.BytesIO(b"b" * 16), io.BytesIO())
    gc.collect()
    print("after re-init:      reader alive=%r writer alive=%r"
          % (wr() is not None, ww() is not None), file=sys.stderr)
    if wr() is not None or ww() is not None:
        print("VERDICT: re-init did NOT release the previous streams -> leak",
              file=sys.stderr)
    else:
        print("VERDICT: re-init released them -> no leak", file=sys.stderr)
    del p
    gc.collect()
    print("after del pair:     reader alive=%r writer alive=%r"
          % (wr() is not None, ww() is not None), file=sys.stderr)


def case_buffered_reinit():
    """BufferedReader.__init__ twice: is the first raw released?"""
    raw1 = io.BytesIO(b"a" * 16)
    b = io.BufferedReader(raw1)
    wr = weakref.ref(raw1)
    del raw1
    gc.collect()
    print("after construction: raw alive=%r" % (wr() is not None,), file=sys.stderr)
    b.__init__(io.BytesIO(b"b" * 16))
    gc.collect()
    print("after re-init:      raw alive=%r" % (wr() is not None,), file=sys.stderr)
    print("VERDICT: %s" % ("LEAK" if wr() is not None else "released"), file=sys.stderr)


def case_textio_reinit():
    """TextIOWrapper.__init__ twice: is the first buffer released?"""
    buf1 = io.BytesIO(b"a" * 16)
    t = io.TextIOWrapper(buf1, encoding="utf-8")
    wr = weakref.ref(buf1)
    del buf1
    gc.collect()
    print("after construction: buffer alive=%r" % (wr() is not None,), file=sys.stderr)
    t.__init__(io.BytesIO(b"b" * 16), encoding="utf-8")
    gc.collect()
    print("after re-init:      buffer alive=%r" % (wr() is not None,), file=sys.stderr)
    print("VERDICT: %s" % ("LEAK" if wr() is not None else "released"), file=sys.stderr)


def case_decoder_store():
    """A user codec factory that re-enters set_decoder: does a decoder leak?

    The re-entrancy is driven from the incrementaldecoder FACTORY (called while
    _textiowrapper_set_decoder is mid-store), not from __init__ of the decoder.
    """
    import codecs

    made = []
    alive = []

    class Dec(codecs.IncrementalDecoder):
        def decode(self, input, final=False):
            return input.decode("utf-8", self.errors)

    def factory(errors="strict"):
        d = Dec(errors)
        made.append(1)
        alive.append(weakref.ref(d))
        if len(made) == 1 and holder[0] is not None:
            try:
                # re-enter while the outer store is in flight
                holder[0].reconfigure(encoding="leaky")
            except BaseException as exc:
                print("    re-entrant reconfigure -> %s" % type(exc).__name__,
                      file=sys.stderr)
        return d

    codecs.register(lambda n: codecs.CodecInfo(
        name="leaky", encode=codecs.utf_8_encode, decode=codecs.utf_8_decode,
        incrementalencoder=codecs.getincrementalencoder("utf-8"),
        incrementaldecoder=factory) if n == "leaky" else None)

    holder = [None]
    holder[0] = io.TextIOWrapper(io.BytesIO(b"abc"), encoding="utf-8")
    try:
        holder[0].reconfigure(encoding="leaky")
    except BaseException as exc:
        print("outer reconfigure -> %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
    holder[0] = None
    gc.collect()
    leaked = sum(1 for w in alive if w() is not None)
    print("decoders constructed=%d still alive=%d" % (len(made), leaked), file=sys.stderr)
    print("VERDICT: %s" % ("LEAK" if leaked else "no leak"), file=sys.stderr)


def case_fileio_writer_leak():
    """fileio.c:828 -- PyBytesWriter_Resize failure path. Needs a short read."""
    import os
    r, w = os.pipe()
    os.write(w, b"hello")
    os.close(w)
    f = io.FileIO(r, "rb", closefd=True)
    before = _refs()
    for _ in range(500):
        f.seek(0) if f.seekable() else None
        try:
            f.readall()
        except BaseException:
            pass
    after = _refs()
    print("readall x500 refcount delta=%s (debug build=%r)" % (after - before, DEBUG),
          file=sys.stderr)
    f.close()


CASES = {k[5:]: v for k, v in sorted(globals().items()) if k.startswith("case_")}


def main():
    if "--case" not in sys.argv:
        print("cases: %s" % " ".join(CASES), file=sys.stderr)
        return 2
    CASES[sys.argv[sys.argv.index("--case") + 1]]()
    return 0


if __name__ == "__main__":
    sys.exit(main())

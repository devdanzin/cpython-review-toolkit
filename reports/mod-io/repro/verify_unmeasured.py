"""Reproduce the mod-io findings whose `reproduced` status rested on agent claims.

Each case is independent; run one per subprocess:

    python verify_unmeasured.py --case <name> [--pyio]

Cases:
  bytesiobuf_closed      CPY-0165  bytesiobuf_getbuffer with no closed check
  fillinfo_leak          CPY-0166  PyBuffer_FillInfo failure ignored -> exports wedged
  seek_null_decoder      CPY-0169  textio.c:2775 decode() on a NULL decoder
  dealloc_warn_swallow   CPY-0173  _dealloc_warn eats a user KeyboardInterrupt
  rwpair_reinit_leak     CONSIDER  BufferedRWPair.__init__ leaks on re-init
  decoder_store_leak     CONSIDER  raw store into self->decoder leaks the re-entrant value
  buffered_lock_sigint   CONSIDER  the buffered lock is uninterruptible
  fileio_index_swallow   CONSIDER  FileIO.__init__ clears a user __index__ exception
  bytesio_dealloc_exc    CONSIDER  bytesio_dealloc mutates exception state unbracketed
  pylong_aslong          CONSIDER  PyLong_AsLong where Py_ssize_t is meant
  textio_init_gil        CONSIDER  TextIOWrapper.__init__ crash, GIL-only per ft-race
"""

import gc
import os
import signal
import sys
import threading

USE_PYIO = "--pyio" in sys.argv
if USE_PYIO:
    import _pyio as io
else:
    import io


# --------------------------------------------------------------- CPY-0165/0166

def case_bytesiobuf_closed():
    """Reach bytesiobuf_getbuffer on a BytesIO whose buf has been freed."""
    b = io.BytesIO(b"x" * 4096)
    mv = b.getbuffer()
    holder = mv.obj              # the bytesiobuf intermediate
    del mv
    gc.collect()
    b.close()                    # frees b->buf
    print("holder=%r; re-exporting" % (type(holder).__name__,), file=sys.stderr)
    memoryview(holder)           # bytesiobuf_getbuffer -> SHARED_BUF on a NULL buf
    print("survived", file=sys.stderr)


def case_fillinfo_leak():
    """Drive PyBuffer_FillInfo to failure and check whether exports is wedged."""
    b = io.BytesIO(b"y" * 64)
    mv = b.getbuffer()
    holder = mv.obj
    del mv
    gc.collect()
    # PEP 688: request a flag combination PyBuffer_FillInfo rejects.
    for flags in (0x100, 0x200, 0x104):
        try:
            holder.__buffer__(flags)
        except BaseException as exc:
            print("__buffer__(%#x) -> %s: %s" % (flags, type(exc).__name__, exc),
                  file=sys.stderr)
        else:
            print("__buffer__(%#x) -> returned a buffer" % flags, file=sys.stderr)
    # If exports leaked, every resize is now permanently refused.
    try:
        b.truncate(1)
        print("truncate after failed exports: OK (no leak)", file=sys.stderr)
    except BufferError as exc:
        print("truncate after failed exports: BufferError (%s) -> EXPORTS WEDGED" % exc,
              file=sys.stderr)


# ------------------------------------------------------------------- CPY-0169

def case_seek_null_decoder():
    """textio.c:2775 -- seek() decodes with self->decoder == NULL."""
    import codecs

    class NoIncrementalDecoder(codecs.CodecInfo):
        pass

    def lookup(name):
        if name != "nodec":
            return None
        return codecs.CodecInfo(
            name="nodec",
            encode=codecs.utf_8_encode,
            decode=codecs.utf_8_decode,
            incrementalencoder=codecs.getincrementalencoder("utf-8"),
            incrementaldecoder=None,          # <- the hole
        )

    codecs.register(lookup)
    t = io.TextIOWrapper(io.BytesIO(b"abcdefghij"), encoding="utf-8")
    t.read(1)
    try:
        t.reconfigure(encoding="nodec")
    except BaseException as exc:
        print("reconfigure -> %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
    cookie = t.tell()
    print("seeking to %r with decoder possibly NULL" % (cookie,), file=sys.stderr)
    t.seek(cookie)
    print("survived", file=sys.stderr)


# ------------------------------------------------------------------- CPY-0173

def case_dealloc_warn_swallow():
    """Does _dealloc_warn swallow a KeyboardInterrupt raised by the warning filter?"""
    import warnings

    seen = []

    def hostile(message, category, filename, lineno, file=None, line=None):
        seen.append(category.__name__)
        raise KeyboardInterrupt("from the ResourceWarning handler")

    warnings.simplefilter("always", ResourceWarning)
    warnings.showwarning = hostile

    unraisable = []
    sys.unraisablehook = lambda a: unraisable.append(repr(a.exc_value))

    raw = io.FileIO(os.devnull, "wb")
    b = io.BufferedWriter(raw)
    b.write(b"z")
    try:
        del b
        gc.collect()
        print("teardown completed, no exception propagated", file=sys.stderr)
    except KeyboardInterrupt as exc:
        print("KeyboardInterrupt PROPAGATED: %s" % exc, file=sys.stderr)
    print("warning handler invoked for: %r" % (seen,), file=sys.stderr)
    print("unraisable reports: %d %r" % (len(unraisable), unraisable[:2]), file=sys.stderr)


# ------------------------------------------------------------------ CONSIDERs

def case_rwpair_reinit_leak():
    """BufferedRWPair.__init__ called twice -- does the first pair leak?"""
    before = len(gc.get_objects())
    p = io.BufferedRWPair(io.BytesIO(b"a"), io.BytesIO())
    for _ in range(200):
        p.__init__(io.BytesIO(b"a"), io.BytesIO())
    del p
    gc.collect()
    after = len(gc.get_objects())
    print("gc objects before=%d after=%d delta=%d" % (before, after, after - before),
          file=sys.stderr)


def case_decoder_store_leak():
    """A user codec factory re-entering set_decoder: does the first decoder leak?"""
    import codecs

    made = []
    finalized = []

    class Dec(codecs.IncrementalDecoder):
        def __init__(self, errors="strict"):
            super().__init__(errors)
            made.append(id(self))
            if len(made) == 1:
                # re-enter while the outer set_decoder is mid-store
                try:
                    t_holder[0].reconfigure(newline="\r")
                except BaseException:
                    pass

        def decode(self, data, final=False):
            return data.decode("utf-8", self.errors)

        def __del__(self):
            finalized.append(1)

    def lookup(name):
        if name != "leaky":
            return None
        return codecs.CodecInfo(name="leaky", encode=codecs.utf_8_encode,
                                decode=codecs.utf_8_decode,
                                incrementalencoder=codecs.getincrementalencoder("utf-8"),
                                incrementaldecoder=Dec)

    codecs.register(lookup)
    t_holder = [None]
    t_holder[0] = io.TextIOWrapper(io.BytesIO(b"abc"), encoding="leaky")
    t_holder[0].read()
    del t_holder[0]
    gc.collect()
    print("decoders constructed=%d finalized=%d leaked=%d"
          % (len(made), len(finalized), len(made) - len(finalized)), file=sys.stderr)


def case_buffered_lock_sigint():
    """Is the buffered lock wait interruptible by SIGINT?"""
    raw = io.BytesIO(b"q" * 64)
    b = io.BufferedReader(raw)

    holding = threading.Event()

    class Slow(io.BytesIO):
        def readinto(self, m):
            holding.set()
            import time
            time.sleep(30)
            return 0

    b2 = io.BufferedReader(Slow(b"z" * 64))
    t = threading.Thread(target=lambda: b2.read(1), daemon=True)
    t.start()
    holding.wait(5)
    os.kill(os.getpid(), signal.SIGINT)
    import time
    t0 = time.monotonic()
    try:
        b2.read(1)                     # blocks on the buffered lock
        print("second read returned after %.2fs" % (time.monotonic() - t0), file=sys.stderr)
    except KeyboardInterrupt:
        print("KeyboardInterrupt after %.2fs -> INTERRUPTIBLE"
              % (time.monotonic() - t0), file=sys.stderr)


def case_fileio_index_swallow():
    """FileIO.__init__ with a hostile __index__: is the exception reported faithfully?"""
    class Evil:
        def __index__(self):
            raise ZeroDivisionError("from __index__")

    try:
        io.FileIO(Evil())
    except BaseException as exc:
        print("FileIO(Evil()) -> %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
    else:
        print("FileIO(Evil()) -> no exception", file=sys.stderr)


def case_bytesio_dealloc_exc():
    """bytesio_dealloc with a live export: does it disturb exception state?"""
    unraisable = []
    sys.unraisablehook = lambda a: unraisable.append((type(a.exc_value).__name__,
                                                      str(a.exc_value)[:60]))

    def make():
        b = io.BytesIO(b"w" * 32)
        mv = b.getbuffer()
        return mv          # b dies with exports > 0

    mv = make()
    gc.collect()
    try:
        raise ValueError("caller's live exception")
    except ValueError:
        del mv
        gc.collect()
        cur = sys.exc_info()[1]
        print("caller exception after dealloc: %r" % (cur,), file=sys.stderr)
    print("unraisable: %r" % (unraisable,), file=sys.stderr)


def case_pylong_aslong():
    """PyLong_AsLong vs Py_ssize_t on truncate: what does a > LONG_MAX size do?"""
    for target, mk in (("BytesIO", lambda: io.BytesIO(b"a" * 8)),
                       ("StringIO", lambda: io.StringIO("a" * 8))):
        o = mk()
        for n in (2**31, 2**63 - 1, 2**64, 2**80):
            try:
                o.truncate(n)
                print("%s.truncate(2**%d-ish) -> OK" % (target, n.bit_length()),
                      file=sys.stderr)
            except BaseException as exc:
                print("%s.truncate(%d) -> %s: %s"
                      % (target, n, type(exc).__name__, str(exc)[:70]), file=sys.stderr)


def case_textio_init_gil():
    """ft-race reported TextIOWrapper.__init__ crashing 5/5 on GIL, 0/5 on FT."""
    class Raw(io.BytesIO):
        def __init__(self, *a):
            super().__init__(*a)
            self.armed = True

        def readable(self):
            if self.armed:
                self.armed = False
                try:
                    holder[0].__init__(io.BytesIO(b"zzz"), encoding="utf-8")
                except BaseException as exc:
                    print("    inner __init__ -> %s" % type(exc).__name__, file=sys.stderr)
            return True

    holder = [None]
    holder[0] = io.TextIOWrapper(Raw(b"abc"), encoding="utf-8")
    print("reading", file=sys.stderr)
    try:
        print("read -> %r" % holder[0].read(), file=sys.stderr)
    except BaseException as exc:
        print("read -> %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
    print("survived", file=sys.stderr)


CASES = {k[5:]: v for k, v in sorted(globals().items()) if k.startswith("case_")}


def main():
    if "--case" not in sys.argv:
        print("cases: %s" % " ".join(CASES), file=sys.stderr)
        return 2
    name = sys.argv[sys.argv.index("--case") + 1]
    if name not in CASES:
        print("unknown case %r" % name, file=sys.stderr)
        return 2
    CASES[name]()
    return 0


if __name__ == "__main__":
    sys.exit(main())

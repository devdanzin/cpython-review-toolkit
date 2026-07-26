"""Residual sites from the 26 scan_init_bypass findings that round 1 and 2
did not settle.

  raw_tell_via_truncate  -- bufferedio.c:788  _buffered_raw_tell
  raw_seek_systemerror   -- bufferedio.c:818  _buffered_raw_seek (soft-guarded
                            by PyObject_CallMethodObjArgs' own NULL check)
  nldecoder_ft_race      -- textio.c:339/551/633  CHECK_INITIALIZED_DECODER
                            tests self->errors, then uses self->decoder.
                            __init__ writes errors BEFORE decoder and is NOT
                            @critical_section, while decode/getstate/reset ARE.
"""

import io
import os
import sys
import threading

if os.environ.get("PYIO"):
    import _pyio as io


def sc_raw_tell_via_truncate():
    """bufferedio.c:788 -- truncate() calls _buffered_raw_tell(self) at :1489
    right after raw.truncate() returned; if THAT call detached, self->raw is
    NULL when _buffered_raw_tell does PyObject_CallMethodNoArgs(self->raw,
    tell)."""

    class EvilRaw:
        closed = False

        def __init__(self):
            self.owner = None

        def writable(self):
            return True

        def readable(self):
            return False

        def seekable(self):
            return True

        def write(self, b):
            return len(b)

        def seek(self, p, w=0):
            return p

        def tell(self):
            return 0

        def flush(self):
            return None

        def truncate(self, pos=None):
            # detach from INSIDE raw.truncate(), i.e. after the :1485 call
            # has already returned successfully.
            try:
                io.BufferedWriter.detach(self.owner)
                sys.stderr.write("  inner detach OK\n")
            except BaseException as e:
                sys.stderr.write("  inner detach failed: %r\n" % (e,))
            return 0

    class Evil(io.BufferedWriter):
        def flush(self):
            return None

    raw = EvilRaw()
    b = Evil(raw, buffer_size=64)
    raw.owner = b
    return b.truncate(0)


def sc_raw_seek_systemerror():
    """bufferedio.c:818 -- same shape, but PyObject_CallMethodObjArgs has its
    own NULL check (Objects/call.c:941), so this degrades to SystemError
    instead of SIGSEGV.  The asymmetry inside the CallMethod family is the
    point."""

    class EvilRaw:
        closed = False
        owner = None

        def writable(self):
            return True

        def readable(self):
            return True

        def seekable(self):
            return True

        def write(self, b):
            o = self.owner
            self.owner = None
            if o is not None:
                try:
                    io.BufferedRandom.detach(o)
                    sys.stderr.write("  inner detach OK\n")
                except BaseException as e:
                    sys.stderr.write("  inner detach failed: %r\n" % (e,))
            return len(b)

        def read(self, n=-1):
            return b""

        def readinto(self, b):
            return 0

        def seek(self, p, w=0):
            return p

        def tell(self):
            return 0

        def flush(self):
            return None

    class Evil(io.BufferedRandom):
        def flush(self):
            return None

    raw = EvilRaw()
    b = Evil(raw, buffer_size=64)
    raw.owner = b
    b.write(b"w" * 40)
    return b.seek(0)


def sc_nldecoder_ft_race():
    """textio.c:551 -- _io_IncrementalNewlineDecoder.getstate does
    CHECK_INITIALIZED_DECODER(self) (which tests self->errors) and then
    PyObject_CallMethodNoArgs(self->decoder, ...).  __init__ (textio.c:261-262)
    stores errors first, decoder second, and is not @critical_section while
    getstate is -- so a concurrent __init__ can be observed in the
    errors!=NULL / decoder==NULL window.  Free-threaded builds only."""
    import codecs

    dec = codecs.getincrementaldecoder("utf-8")()
    d = io.IncrementalNewlineDecoder(dec, True)
    stop = [False]

    def reinit():
        while not stop[0]:
            d.__init__(dec, True, "strict")

    def poke():
        for _ in range(400000):
            try:
                d.getstate()
                d.reset()
                d.decode(b"a")
            except Exception:
                pass

    ts = [threading.Thread(target=reinit) for _ in range(3)]
    ts += [threading.Thread(target=poke) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts[3:]:
        t.join()
    stop[0] = True
    for t in ts[:3]:
        t.join()
    return "no crash"


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
        sys.stderr.write("SURVIVED -> %.80r\n" % (out,))
    except BaseException as exc:
        sys.stderr.write("RAISED %s: %s\n" % (type(exc).__name__, exc))

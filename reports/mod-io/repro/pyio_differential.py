"""Differential oracle: run each mod-io memory finding against BOTH backends.

    import io          -> the C accelerator (Modules/_io)
    import _pyio as io -> the shipped pure-Python twin (Lib/_pyio.py)

Grading (AGENT_BRIEF section 2):
    C crashes / _pyio raises cleanly  -> confirmed, localized C bug (FIX)
    both crash                        -> below _io, out of scope
    same behaviour                    -> not a finding

Usage: <python> pyio_differential.py <case> <backend>
  case    = truncate_index | buf_after_close | fillinfo | reinit_read
            | reinit_write | reinit_lock
  backend = io | _pyio
Exit code carries the verdict; stdout carries the observed behaviour.
"""
import sys

CASE = sys.argv[1]
BACKEND = sys.argv[2]

if BACKEND == "_pyio":
    import _pyio as io
else:
    import io

BIG = 1 << 20
SMALL = 64


def say(*a):
    print(*a)
    sys.stdout.flush()


def truncate_index():
    holder = {}

    class Evil:
        def __index__(self):
            holder["v"] = holder["b"].getbuffer()
            return 0

    b = io.BytesIO(b"A" * 200000)
    holder["b"] = b
    try:
        say("truncate ->", b.truncate(Evil()))
    except BaseException as e:
        say("truncate raised", type(e).__name__, e)
    v = holder.get("v")
    if v is not None:
        say("view len", len(v), "-> reading")
        say("checksum", sum(bytes(v)[:1024]))
        v[0:1] = b"Z"
        say("wrote through view")
    say("getvalue len", len(b.getvalue()))


def buf_after_close():
    b = io.BytesIO(b"hello world" * 100)
    m = b.getbuffer()
    inner = m.obj
    say("inner type", type(inner).__name__)
    m.release()
    b.close()
    m2 = memoryview(inner)
    say("re-export survived, len", len(m2))


def fillinfo():
    b = io.BytesIO(b"payload" * 64)
    m = b.getbuffer()
    inner = m.obj
    m.release()
    try:
        got = inner.__buffer__(0x100)   # PyBUF_READ
        say("__buffer__(0x100) ->", type(got).__name__)
    except BaseException as e:
        say("__buffer__(0x100) raised", type(e).__name__, e)
    try:
        b.truncate(0)
        say("POST truncate(0) ok")
    except BaseException as e:
        say("POST truncate(0) raised", type(e).__name__, e)


def reinit_read():
    class Sink(io.RawIOBase):
        def readable(self):
            return True

        def readinto(self, b):
            return 0

    class Evil(io.RawIOBase):
        def readable(self):
            return True

        def readinto(self, b):
            n = len(b)
            br.__init__(Sink(), buffer_size=SMALL)
            b[0:n] = b"X" * n
            return n

    global br
    br = io.BufferedReader(Evil(), buffer_size=BIG)
    say("peek ->", len(br.peek()))


def reinit_write():
    class Sink(io.RawIOBase):
        def writable(self):
            return True

        def seekable(self):
            return True

        def write(self, b):
            return len(b)

    class Evil(io.RawIOBase):
        def writable(self):
            return True

        def seekable(self):
            return True

        def write(self, b):
            n = len(b)
            bw.__init__(Sink(), buffer_size=SMALL)
            bytes(b)
            return n

    global bw
    bw = io.BufferedWriter(Evil(), buffer_size=BIG)
    bw.write(b"Z" * (BIG - 1))
    bw.flush()
    say("flush survived")


def reinit_lock():
    class Sink(io.RawIOBase):
        def readable(self):
            return True

        def readinto(self, b):
            return 0

    class Evil(io.RawIOBase):
        def readable(self):
            return True

        def readinto(self, b):
            br.__init__(Sink(), buffer_size=BIG)
            return 0

    global br
    br = io.BufferedReader(Evil(), buffer_size=BIG)
    say("peek ->", len(br.peek()))
    say("peek2 ->", len(br.peek()))
    br.close()
    say("closed")


def nldecoder_self():
    import codecs

    st = {"fired": 0, "junk": None}

    class D:
        def __init__(self, errors="strict"):
            self.errors = errors

        def decode(self, data, final=False):
            if not st["fired"]:
                st["fired"] = 1
                f.reconfigure(newline="\r")
                st["junk"] = [bytearray(64) for _ in range(8000)]
            return bytes(data).decode("latin-1")

        def getstate(self):
            return (b"", 0)

        def setstate(self, s):
            pass

        def reset(self):
            pass

    class E:
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
            incrementalencoder=E,
            incrementaldecoder=D,
        )

    codecs.register(search)
    global f
    raw = io.BufferedReader(io.BytesIO(b"abcdefghijklmnopqrstuvwxyz" * 64))
    f = io.TextIOWrapper(raw, encoding="uaf_test_codec")
    say("read ->", repr(f.read(5)))


CASES = {
    "nldecoder_self": nldecoder_self,
    "truncate_index": truncate_index,
    "buf_after_close": buf_after_close,
    "fillinfo": fillinfo,
    "reinit_read": reinit_read,
    "reinit_write": reinit_write,
    "reinit_lock": reinit_lock,
}

try:
    CASES[CASE]()
    say("== completed ==")
except BaseException as e:
    say("== raised", type(e).__name__ + ":", e, "==")

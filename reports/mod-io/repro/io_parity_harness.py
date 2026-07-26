#!/usr/bin/env python3
"""Dual-backend differential harness for the _io / _pyio twin pair.

CPython ships `Modules/_io` (C accelerator) and `Lib/_pyio.py` (pure-Python
reimplementation of the same public API).  Every case below is run twice, in
two separate subprocesses, with the *same* body but a different backend bound
to the name `io`:

    C   backend:  import io            -> _io  (Modules/_io/*.c)
    py  backend:  import _pyio as io   -> Lib/_pyio.py

A body is a snippet of Python that has `io`, `sys`, `os` and `emit()` in scope.
It should `emit(...)` whatever it wants compared and let exceptions escape.

Grading (from reports/mod-io/preflight/AGENT_BRIEF.md section 2):

    C SIGSEGV/SIGABRT + twin raises cleanly  -> C_CRASH        (FIX)
    C SystemError/fatal + twin normal        -> C_CONTRACT     (FIX)
    C raises X, twin raises Y                -> EXC_DIFF       (CONSIDER)
    C prints X, twin prints Y                -> OUTPUT_DIFF    (CONSIDER)
    both crash                               -> BOTH_CRASH     (not an _io bug)
    same                                     -> AGREE

Usage:
    python io_parity_harness.py [--python INTERP] [--filter SUBSTR]
                                [--group G] [--timeout SEC] [--verbose]
                                [--repeat N]

The harness is deliberately self-contained (stdlib only) and interpreter-
agnostic: point --python at any CPython that ships both backends.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import textwrap

# --------------------------------------------------------------------------
# Prelude injected into every child process.
# --------------------------------------------------------------------------

PRELUDE = """\
import sys, os, faulthandler, tempfile
faulthandler.enable()
{importline}

_OUT = []
def emit(*a):
    _OUT.append(" ".join(repr(x) if not isinstance(x, str) else x for x in a))

def tmpfile(data=b""):
    fd, path = tempfile.mkstemp()
    os.write(fd, data)
    os.close(fd)
    return path

class LyingIndex:
    def __init__(self, v):
        self.v = v
    def __index__(self):
        return self.v

class RaisingIndex:
    def __index__(self):
        raise RuntimeError("boom from __index__")

class NonIntIndex:
    def __index__(self):
        return "not an int"

def _flush():
    sys.stdout.write("\\n".join(_OUT))
    sys.stdout.flush()

import atexit
atexit.register(_flush)
"""

C_IMPORT = "import io"
PY_IMPORT = "import _pyio as io"


# --------------------------------------------------------------------------
# Cases.  (name, group, body)
# --------------------------------------------------------------------------

CASES: list[tuple[str, str, str]] = []


def case(name: str, group: str, body: str) -> None:
    CASES.append((name, group, textwrap.dedent(body)))


# ---------------------------------------------------------------- group 1:
# RE-ENTRANCY.  A subclass whose flush/read/write/close/readable/seekable
# re-enters the same object.  This is where the C code is provably weak.
# ---------------------------------------------------------------------------

case("reent-detach-bufferedreader", "reentrancy", """
    class Evil(io.BufferedReader):
        armed = True
        def flush(self):
            if self.armed:
                self.armed = False
                emit("inner detach ->", type(super().detach()).__name__)
    e = Evil(io.BytesIO(b"abc"))
    emit("outer detach ->", repr(e.detach()))
""")

case("reent-detach-bufferedwriter", "reentrancy", """
    class Evil(io.BufferedWriter):
        armed = True
        def flush(self):
            if self.armed:
                self.armed = False
                emit("inner detach ->", type(super().detach()).__name__)
    e = Evil(io.BytesIO())
    emit("outer detach ->", repr(e.detach()))
""")

case("reent-detach-bufferedrandom", "reentrancy", """
    class Evil(io.BufferedRandom):
        armed = True
        def flush(self):
            if self.armed:
                self.armed = False
                emit("inner detach ->", type(super().detach()).__name__)
    e = Evil(io.BytesIO(b"abc"))
    emit("outer detach ->", repr(e.detach()))
""")

case("reent-detach-textio", "reentrancy", """
    class Evil(io.TextIOWrapper):
        armed = True
        def flush(self):
            if self.armed:
                self.armed = False
                emit("inner detach ->", type(super().detach()).__name__)
    e = Evil(io.BytesIO(b"abc"))
    emit("outer detach ->", repr(e.detach()))
""")

case("reent-close-in-flush-buffered", "reentrancy", """
    class Evil(io.BufferedWriter):
        armed = True
        def flush(self):
            if self.armed:
                self.armed = False
                super().close()
                emit("inner close done")
    e = Evil(io.BytesIO())
    e.write(b"xyz")
    e.close()
    emit("survived closed=", e.closed)
""")

case("reent-close-in-flush-textio", "reentrancy", """
    class Evil(io.TextIOWrapper):
        armed = True
        def flush(self):
            if self.armed:
                self.armed = False
                super().close()
                emit("inner close done")
    e = Evil(io.BytesIO())
    e.write("xyz")
    e.close()
    emit("survived closed=", e.closed)
""")

case("reent-detach-in-close-textio", "reentrancy", """
    class Evil(io.TextIOWrapper):
        armed = True
        def flush(self):
            if self.armed:
                self.armed = False
                emit("inner detach ->", type(super().detach()).__name__)
    e = Evil(io.BytesIO())
    e.close()
    emit("survived closed=", e.closed)
""")

case("reent-detach-in-close-buffered", "reentrancy", """
    class Evil(io.BufferedWriter):
        armed = True
        def flush(self):
            if self.armed:
                self.armed = False
                emit("inner detach ->", type(super().detach()).__name__)
    e = Evil(io.BytesIO())
    e.close()
    emit("survived closed=", e.closed)
""")

case("reent-detach-from-raw-write", "reentrancy", """
    holder = {}
    class Raw(io.RawIOBase):
        def writable(self): return True
        def write(self, b):
            w = holder.get("w")
            if w is not None and not holder.get("fired"):
                holder["fired"] = True
                try:
                    emit("inner detach ->", type(w.detach()).__name__)
                except Exception as exc:
                    emit("inner detach raised", type(exc).__name__)
            return len(b)
    r = Raw()
    w = io.BufferedWriter(r, buffer_size=8)
    holder["w"] = w
    w.write(b"0123456789012345")
    emit("after write")
    w.flush()
    emit("survived")
""")

case("reent-detach-from-raw-readinto", "reentrancy", """
    holder = {}
    class Raw(io.RawIOBase):
        def readable(self): return True
        def readinto(self, b):
            br = holder.get("br")
            if br is not None and not holder.get("fired"):
                holder["fired"] = True
                try:
                    emit("inner detach ->", type(br.detach()).__name__)
                except Exception as exc:
                    emit("inner detach raised", type(exc).__name__)
            b[0:3] = b"abc"
            return 3
    r = Raw()
    br = io.BufferedReader(r, buffer_size=8)
    holder["br"] = br
    emit("read ->", br.read(3))
    emit("survived")
""")

case("reent-close-from-raw-readable", "reentrancy", """
    holder = {}
    class Raw(io.RawIOBase):
        def readable(self):
            br = holder.get("br")
            if br is not None and not holder.get("fired"):
                holder["fired"] = True
                try:
                    br.close()
                    emit("inner close ok")
                except Exception as exc:
                    emit("inner close raised", type(exc).__name__)
            return True
    r = Raw()
    br = io.BufferedReader.__new__(io.BufferedReader)
    holder["br"] = br
    br.__init__(r)
    emit("survived", br.closed)
""")

case("reent-reinit-buffered-during-flush", "reentrancy", """
    class Evil(io.BufferedWriter):
        armed = True
        def flush(self):
            if self.armed:
                self.armed = False
                # re-run __init__ on a live buffered object mid-flush
                io.BufferedWriter.__init__(self, io.BytesIO(), buffer_size=4)
                emit("re-init done")
            return super().flush()
    e = Evil(io.BytesIO(), buffer_size=1024)
    e.write(b"a" * 32)
    e.flush()
    emit("survived")
""")

case("reent-reinit-textio-during-flush", "reentrancy", """
    class Evil(io.TextIOWrapper):
        armed = True
        def flush(self):
            if self.armed:
                self.armed = False
                io.TextIOWrapper.__init__(self, io.BytesIO(), encoding="utf-8")
                emit("re-init done")
            return super().flush()
    e = Evil(io.BytesIO(), encoding="utf-8")
    e.write("a" * 32)
    e.flush()
    emit("survived")
""")

case("reent-seek-in-flush", "reentrancy", """
    class Evil(io.BufferedRandom):
        armed = True
        def flush(self):
            if self.armed:
                self.armed = False
                super().seek(0)
                emit("inner seek done")
            return super().flush()
    e = Evil(io.BytesIO(b"0123456789"))
    e.write(b"XY")
    emit("seek ->", e.seek(5))
    emit("survived")
""")

case("reent-truncate-in-flush", "reentrancy", """
    class Evil(io.BufferedRandom):
        armed = True
        def flush(self):
            if self.armed:
                self.armed = False
                emit("inner detach ->", type(super().detach()).__name__)
            return None
    e = Evil(io.BytesIO(b"0123456789"))
    emit("truncate ->", e.truncate(3))
    emit("survived")
""")

case("reent-tell-in-flush", "reentrancy", """
    class Evil(io.BufferedRandom):
        armed = True
        def flush(self):
            if self.armed:
                self.armed = False
                emit("inner detach ->", type(super().detach()).__name__)
            return None
    e = Evil(io.BytesIO(b"0123456789"))
    emit("tell ->", e.tell())
    emit("survived")
""")

case("reent-rwpair-detach", "reentrancy", """
    class R(io.BytesIO):
        pass
    class W(io.BytesIO):
        armed = True
        def flush(self):
            if self.armed and holder.get("p") is not None:
                self.armed = False
                try:
                    holder["p"].close()
                    emit("inner close ok")
                except Exception as exc:
                    emit("inner close raised", type(exc).__name__)
    holder = {}
    p = io.BufferedRWPair(R(b"abc"), W())
    holder["p"] = p
    p.write(b"z")
    p.flush()
    emit("survived")
""")

case("reent-textio-writeflush-close-buffer", "reentrancy", """
    class Buf(io.BytesIO):
        armed = True
        def write(self, b):
            if self.armed and holder.get("t") is not None:
                self.armed = False
                try:
                    holder["t"].detach()
                    emit("inner detach ok")
                except Exception as exc:
                    emit("inner detach raised", type(exc).__name__)
            return super().write(b)
    holder = {}
    b = Buf()
    t = io.TextIOWrapper(b, encoding="utf-8", write_through=False)
    holder["t"] = t
    t.write("hello")
    t.flush()
    emit("survived")
""")

case("reent-textio-seek-buffer-detach", "reentrancy", """
    class Buf(io.BytesIO):
        armed = True
        def seekable(self): return True
        def seek(self, *a):
            if self.armed and holder.get("t") is not None:
                self.armed = False
                try:
                    holder["t"].detach()
                    emit("inner detach ok")
                except Exception as exc:
                    emit("inner detach raised", type(exc).__name__)
            return super().seek(*a)
    holder = {}
    b = Buf(b"abcdef")
    t = io.TextIOWrapper(b, encoding="utf-8")
    holder["t"] = t
    emit("seek ->", t.seek(0))
    emit("survived")
""")

case("reent-del-during-close", "reentrancy", """
    import gc
    holder = {}
    class Evil(io.BufferedWriter):
        def flush(self):
            holder["junk"] = None
            gc.collect()
            return super().flush()
    e = Evil(io.BytesIO())
    class Junk:
        def __del__(self):
            try:
                e.close()
            except Exception as exc:
                emit("del close raised", type(exc).__name__)
    holder["junk"] = Junk()
    e.write(b"abc")
    e.close()
    emit("survived")
""")

case("reent-newinit-race-textio", "reentrancy", """
    t = io.TextIOWrapper.__new__(io.TextIOWrapper)
    try:
        emit("uninit read ->", t.read())
    except Exception as exc:
        emit("read raised", type(exc).__name__)
    try:
        emit("uninit tell ->", t.tell())
    except Exception as exc:
        emit("tell raised", type(exc).__name__)
    try:
        emit("uninit detach ->", t.detach())
    except Exception as exc:
        emit("detach raised", type(exc).__name__)
""")

case("reent-newinit-buffered", "reentrancy", """
    for cls in (io.BufferedReader, io.BufferedWriter, io.BufferedRandom, io.BufferedRWPair):
        b = cls.__new__(cls)
        for meth in ("read", "readable", "close", "detach", "tell", "fileno"):
            try:
                r = getattr(b, meth)()
                emit(cls.__name__, meth, "->", repr(r))
            except Exception as exc:
                emit(cls.__name__, meth, "raised", type(exc).__name__)
""")

case("reent-newinit-fileio-bytesio-stringio", "reentrancy", """
    for cls in (io.FileIO, io.BytesIO, io.StringIO):
        o = cls.__new__(cls)
        for meth in ("read", "readable", "close", "tell", "fileno", "seekable"):
            try:
                r = getattr(o, meth)()
                emit(cls.__name__, meth, "->", repr(r))
            except Exception as exc:
                emit(cls.__name__, meth, "raised", type(exc).__name__)
""")

# ---------------------------------------------------------------- group 2:
# LYING OBJECTS.  A raw whose readinto() returns a bogus count is a buffer
# overflow primitive if unchecked.
# ---------------------------------------------------------------------------

case("lie-readinto-overlong", "lying", """
    class Raw(io.RawIOBase):
        def readable(self): return True
        def readinto(self, b):
            b[0:4] = b"abcd"
            return 1 << 30          # far larger than the buffer
    br = io.BufferedReader(Raw(), buffer_size=8)
    emit("read ->", br.read(4))
""")

case("lie-readinto-overlong-small", "lying", """
    class Raw(io.RawIOBase):
        def readable(self): return True
        def readinto(self, b):
            b[0:4] = b"abcd"
            return len(b) + 16      # slightly larger than the buffer
    br = io.BufferedReader(Raw(), buffer_size=8)
    emit("read ->", br.read(4))
""")

case("lie-readinto-negative", "lying", """
    class Raw(io.RawIOBase):
        def readable(self): return True
        def readinto(self, b):
            return -5
    br = io.BufferedReader(Raw(), buffer_size=8)
    emit("read ->", br.read(4))
""")

case("lie-readinto-huge-index", "lying", """
    class Raw(io.RawIOBase):
        def readable(self): return True
        def readinto(self, b):
            return LyingIndex(1 << 100)
    br = io.BufferedReader(Raw(), buffer_size=8)
    emit("read ->", br.read(4))
""")

case("lie-readinto-nonint", "lying", """
    class Raw(io.RawIOBase):
        def readable(self): return True
        def readinto(self, b):
            return "three"
    br = io.BufferedReader(Raw(), buffer_size=8)
    emit("read ->", br.read(4))
""")

case("lie-readinto-raises", "lying", """
    class Raw(io.RawIOBase):
        def readable(self): return True
        def readinto(self, b):
            raise RuntimeError("boom in readinto")
    br = io.BufferedReader(Raw(), buffer_size=8)
    emit("read ->", br.read(4))
""")

case("lie-readall-nonbytes", "lying", """
    class Raw(io.RawIOBase):
        def readable(self): return True
        def readall(self): return "a str, not bytes"
    br = io.BufferedReader(Raw(), buffer_size=8)
    emit("read ->", br.read())
""")

case("lie-read-longer-than-requested", "lying", """
    class Raw(io.RawIOBase):
        def readable(self): return True
        def read(self, n=-1): return b"x" * (n + 1000 if n and n > 0 else 1000)
        def readinto(self, b):
            data = self.read(len(b))
            b[:len(b)] = data[:len(b)]
            return len(b)
    br = io.BufferedReader(Raw(), buffer_size=8)
    emit("read ->", len(br.read(4)))
""")

case("lie-rawread-returns-str", "lying", """
    class Raw(io.RawIOBase):
        def readable(self): return True
        def read(self, n=-1): return "not bytes"
        def readinto(self, b): return None
    br = io.BufferedReader(Raw(), buffer_size=8)
    emit("read ->", br.read(4))
""")

case("lie-write-returns-overlong", "lying", """
    class Raw(io.RawIOBase):
        def writable(self): return True
        def write(self, b): return len(b) + 1000
    w = io.BufferedWriter(Raw(), buffer_size=8)
    w.write(b"0123456789012345")
    w.flush()
    emit("survived")
""")

case("lie-write-returns-negative", "lying", """
    class Raw(io.RawIOBase):
        def writable(self): return True
        def write(self, b): return -3
    w = io.BufferedWriter(Raw(), buffer_size=8)
    w.write(b"0123456789012345")
    w.flush()
    emit("survived")
""")

case("lie-write-returns-nonint", "lying", """
    class Raw(io.RawIOBase):
        def writable(self): return True
        def write(self, b): return object()
    w = io.BufferedWriter(Raw(), buffer_size=8)
    w.write(b"0123456789012345")
    w.flush()
    emit("survived")
""")

case("lie-write-returns-huge-index", "lying", """
    class Raw(io.RawIOBase):
        def writable(self): return True
        def write(self, b): return LyingIndex(1 << 100)
    w = io.BufferedWriter(Raw(), buffer_size=8)
    w.write(b"0123456789012345")
    w.flush()
    emit("survived")
""")

case("lie-seek-returns-negative", "lying", """
    class Raw(io.RawIOBase):
        def readable(self): return True
        def seekable(self): return True
        def seek(self, pos, whence=0): return -12345
        def tell(self): return -12345
        def readinto(self, b): return 0
    br = io.BufferedReader(Raw(), buffer_size=8)
    emit("seek ->", br.seek(0))
    emit("tell ->", br.tell())
""")

case("lie-seek-returns-huge", "lying", """
    class Raw(io.RawIOBase):
        def readable(self): return True
        def seekable(self): return True
        def seek(self, pos, whence=0): return 1 << 100
        def tell(self): return 1 << 100
        def readinto(self, b): return 0
    br = io.BufferedReader(Raw(), buffer_size=8)
    emit("seek ->", br.seek(0))
""")

case("lie-tell-returns-nonint", "lying", """
    class Raw(io.RawIOBase):
        def readable(self): return True
        def seekable(self): return True
        def tell(self): return "here"
        def readinto(self, b): return 0
    br = io.BufferedReader(Raw(), buffer_size=8)
    emit("tell ->", br.tell())
""")

case("lie-bytesio-read-index-huge", "lying", """
    b = io.BytesIO(b"abcdef")
    emit("read ->", b.read(LyingIndex(1 << 100)))
""")

case("lie-bytesio-read-index-neg", "lying", """
    b = io.BytesIO(b"abcdef")
    emit("read ->", b.read(LyingIndex(-(1 << 100))))
""")

case("lie-bytesio-read-index-raises", "lying", """
    b = io.BytesIO(b"abcdef")
    emit("read ->", b.read(RaisingIndex()))
""")

case("lie-bytesio-read-index-nonint", "lying", """
    b = io.BytesIO(b"abcdef")
    emit("read ->", b.read(NonIntIndex()))
""")

case("lie-bytesio-truncate-index", "lying", """
    b = io.BytesIO(b"abcdef")
    for v in (-1, -(1 << 100), 1 << 100):
        try:
            emit("truncate", v, "->", b.truncate(LyingIndex(v)))
        except Exception as exc:
            emit("truncate", v, "raised", type(exc).__name__, str(exc))
""")

case("lie-bytesio-seek-index", "lying", """
    b = io.BytesIO(b"abcdef")
    for v in (-1, 1 << 100, -(1 << 100)):
        try:
            emit("seek", v, "->", b.seek(LyingIndex(v)))
        except Exception as exc:
            emit("seek", v, "raised", type(exc).__name__)
    for w in (-1, 3, 1 << 100):
        try:
            emit("whence", w, "->", b.seek(0, w))
        except Exception as exc:
            emit("whence", w, "raised", type(exc).__name__)
""")

case("lie-stringio-index", "lying", """
    s = io.StringIO("abcdef")
    for v in (-1, 1 << 100):
        for meth in ("read", "seek", "truncate"):
            try:
                emit(meth, v, "->", repr(getattr(s, meth)(LyingIndex(v))))
            except Exception as exc:
                emit(meth, v, "raised", type(exc).__name__)
""")

case("lie-buffersize-index", "lying", """
    for v in (0, -1, 1 << 100, -(1 << 100)):
        try:
            b = io.BufferedReader(io.BytesIO(b"abc"), buffer_size=LyingIndex(v))
            emit("bufsize", v, "-> ok")
        except Exception as exc:
            emit("bufsize", v, "raised", type(exc).__name__)
""")

case("lie-readinto-buffer-len", "lying", """
    class Weird:
        def __len__(self): return -1
    b = io.BytesIO(b"abcdef")
    try:
        emit("readinto ->", b.readinto(bytearray(4)))
    except Exception as exc:
        emit("readinto raised", type(exc).__name__)
    try:
        emit("readinto weird ->", b.readinto(Weird()))
    except Exception as exc:
        emit("readinto weird raised", type(exc).__name__)
""")

case("lie-writelines-lying-iterable", "lying", """
    class It:
        def __iter__(self): return self
        def __next__(self):
            raise RuntimeError("boom mid-writelines")
    b = io.BytesIO()
    try:
        b.writelines(It())
    except Exception as exc:
        emit("writelines raised", type(exc).__name__)
    emit("value", b.getvalue())
""")

case("lie-writelines-mutating", "lying", """
    b = io.BytesIO()
    def gen():
        yield b"a"
        b.close()
        yield b"b"
    try:
        b.writelines(gen())
    except Exception as exc:
        emit("writelines raised", type(exc).__name__)
    emit("survived")
""")

case("lie-fileno-huge", "lying", """
    class Raw(io.RawIOBase):
        def readable(self): return True
        def fileno(self): return 1 << 100
    br = io.BufferedReader(Raw())
    try:
        emit("fileno ->", br.fileno())
    except Exception as exc:
        emit("fileno raised", type(exc).__name__)
""")

case("lie-fileio-fd-index", "lying", """
    for v in (-1, 1 << 100, -(1 << 100)):
        try:
            f = io.FileIO(LyingIndex(v), "r", closefd=False)
            emit("fd", v, "-> ok")
        except Exception as exc:
            emit("fd", v, "raised", type(exc).__name__)
""")

case("lie-fileio-truncate-index", "lying", """
    p = tmpfile(b"0123456789")
    f = io.FileIO(p, "r+")
    for v in (-1, 1 << 100):
        try:
            emit("truncate", v, "->", f.truncate(LyingIndex(v)))
        except Exception as exc:
            emit("truncate", v, "raised", type(exc).__name__)
    f.close()
    os.unlink(p)
""")

case("lie-strsubclass-write", "lying", """
    class S(str):
        def __str__(self): return "lie"
    s = io.StringIO()
    emit("write ->", s.write(S("real")))
    emit("value ->", s.getvalue())
""")

case("lie-bytes-subclass-write", "lying", """
    class B(bytes):
        def __len__(self): return 9999
    b = io.BytesIO()
    try:
        emit("write ->", b.write(B(b"real")))
    except Exception as exc:
        emit("write raised", type(exc).__name__)
    emit("value ->", b.getvalue())
""")

# ---------------------------------------------------------------- group 3:
# CODEC ATTACKS.  TextIOWrapper drives a user-replaceable codec, and
# tell()/seek() carry the most intricate state in the module.
# ---------------------------------------------------------------------------

CODEC_PRELUDE = """
    import codecs
    def register(name, dec_cls=None, enc_cls=None):
        base = codecs.lookup("utf-8")
        D = dec_cls or base.incrementaldecoder
        E = enc_cls or base.incrementalencoder
        info = codecs.CodecInfo(
            name=name, encode=base.encode, decode=base.decode,
            incrementalencoder=E, incrementaldecoder=D,
            streamreader=base.streamreader, streamwriter=base.streamwriter)
        codecs.register(lambda n: info if n == name else None)
        return name
"""

case("codec-decoder-returns-nonstr", "codec", CODEC_PRELUDE + """
    class D:
        def __init__(self, errors="strict"): pass
        def decode(self, b, final=False): return b"bytes not str"
        def reset(self): pass
        def getstate(self): return (b"", 0)
        def setstate(self, s): pass
    n = register("evil-nonstr", dec_cls=D)
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding=n)
    emit("read ->", t.read())
""")

case("codec-decoder-returns-int", "codec", CODEC_PRELUDE + """
    class D:
        def __init__(self, errors="strict"): pass
        def decode(self, b, final=False): return 12345
        def reset(self): pass
        def getstate(self): return (b"", 0)
        def setstate(self, s): pass
    n = register("evil-int", dec_cls=D)
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding=n)
    emit("read ->", t.read())
""")

case("codec-decoder-raises", "codec", CODEC_PRELUDE + """
    class D:
        def __init__(self, errors="strict"): pass
        def decode(self, b, final=False): raise RuntimeError("boom in decode")
        def reset(self): pass
        def getstate(self): return (b"", 0)
        def setstate(self, s): pass
    n = register("evil-raise", dec_cls=D)
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding=n)
    emit("read ->", t.read())
""")

case("codec-decoder-detaches-midread", "codec", CODEC_PRELUDE + """
    holder = {}
    class D:
        def __init__(self, errors="strict"): self.armed = True
        def decode(self, b, final=False):
            t = holder.get("t")
            if t is not None and self.armed:
                self.armed = False
                try:
                    t.detach()
                    emit("inner detach ok")
                except Exception as exc:
                    emit("inner detach raised", type(exc).__name__)
            return b.decode("utf-8")
        def reset(self): pass
        def getstate(self): return (b"", 0)
        def setstate(self, s): pass
    n = register("evil-detach", dec_cls=D)
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding=n)
    holder["t"] = t
    emit("read ->", t.read())
    emit("survived")
""")

case("codec-decoder-closes-midread", "codec", CODEC_PRELUDE + """
    holder = {}
    class D:
        def __init__(self, errors="strict"): self.armed = True
        def decode(self, b, final=False):
            t = holder.get("t")
            if t is not None and self.armed:
                self.armed = False
                try:
                    t.close()
                    emit("inner close ok")
                except Exception as exc:
                    emit("inner close raised", type(exc).__name__)
            return b.decode("utf-8")
        def reset(self): pass
        def getstate(self): return (b"", 0)
        def setstate(self, s): pass
    n = register("evil-close", dec_cls=D)
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding=n)
    holder["t"] = t
    try:
        emit("read ->", t.read())
    except Exception as exc:
        emit("read raised", type(exc).__name__)
    emit("survived")
""")

case("codec-decoder-reconfigures-midread", "codec", CODEC_PRELUDE + """
    holder = {}
    class D:
        def __init__(self, errors="strict"): self.armed = True
        def decode(self, b, final=False):
            t = holder.get("t")
            if t is not None and self.armed:
                self.armed = False
                try:
                    t.reconfigure(encoding="latin-1")
                    emit("inner reconfigure ok")
                except Exception as exc:
                    emit("inner reconfigure raised", type(exc).__name__)
            return b.decode("utf-8")
        def reset(self): pass
        def getstate(self): return (b"", 0)
        def setstate(self, s): pass
    n = register("evil-reconf", dec_cls=D)
    t = io.TextIOWrapper(io.BytesIO(b"abcdef" * 100), encoding=n)
    holder["t"] = t
    try:
        emit("read ->", len(t.read()))
    except Exception as exc:
        emit("read raised", type(exc).__name__)
    emit("survived")
""")

case("codec-getstate-nontuple", "codec", CODEC_PRELUDE + """
    class D:
        def __init__(self, errors="strict"): self.d = None
        def decode(self, b, final=False): return b.decode("utf-8")
        def reset(self): pass
        def getstate(self): return "not a tuple"
        def setstate(self, s): pass
    n = register("evil-gs1", dec_cls=D)
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding=n)
    t.read(1)
    emit("tell ->", t.tell())
""")

case("codec-getstate-wrong-arity", "codec", CODEC_PRELUDE + """
    class D:
        def __init__(self, errors="strict"): pass
        def decode(self, b, final=False): return b.decode("utf-8")
        def reset(self): pass
        def getstate(self): return (b"", 0, 0, 0)
        def setstate(self, s): pass
    n = register("evil-gs2", dec_cls=D)
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding=n)
    t.read(1)
    emit("tell ->", t.tell())
""")

case("codec-getstate-huge-int", "codec", CODEC_PRELUDE + """
    class D:
        def __init__(self, errors="strict"): pass
        def decode(self, b, final=False): return b.decode("utf-8")
        def reset(self): pass
        def getstate(self): return (b"", 1 << 100)
        def setstate(self, s): pass
    n = register("evil-gs3", dec_cls=D)
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding=n)
    t.read(1)
    emit("tell ->", t.tell())
""")

case("codec-getstate-negative-int", "codec", CODEC_PRELUDE + """
    class D:
        def __init__(self, errors="strict"): pass
        def decode(self, b, final=False): return b.decode("utf-8")
        def reset(self): pass
        def getstate(self): return (b"", -1)
        def setstate(self, s): pass
    n = register("evil-gs4", dec_cls=D)
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding=n)
    t.read(1)
    emit("tell ->", t.tell())
""")

case("codec-getstate-nonbytes-buffer", "codec", CODEC_PRELUDE + """
    class D:
        def __init__(self, errors="strict"): pass
        def decode(self, b, final=False): return b.decode("utf-8")
        def reset(self): pass
        def getstate(self): return (12345, 0)
        def setstate(self, s): pass
    n = register("evil-gs5", dec_cls=D)
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding=n)
    t.read(1)
    emit("tell ->", t.tell())
""")

case("codec-getstate-huge-bytes", "codec", CODEC_PRELUDE + """
    class D:
        def __init__(self, errors="strict"): pass
        def decode(self, b, final=False): return b.decode("utf-8")
        def reset(self): pass
        def getstate(self): return (b"x" * 4096, 0)
        def setstate(self, s): pass
    n = register("evil-gs6", dec_cls=D)
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding=n)
    t.read(1)
    emit("tell ->", t.tell())
""")

case("codec-getstate-raises", "codec", CODEC_PRELUDE + """
    class D:
        def __init__(self, errors="strict"): pass
        def decode(self, b, final=False): return b.decode("utf-8")
        def reset(self): pass
        def getstate(self): raise RuntimeError("boom in getstate")
        def setstate(self, s): pass
    n = register("evil-gs7", dec_cls=D)
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding=n)
    t.read(1)
    emit("tell ->", t.tell())
""")

case("codec-getstate-detaches", "codec", CODEC_PRELUDE + """
    holder = {}
    class D:
        def __init__(self, errors="strict"): self.armed = True
        def decode(self, b, final=False): return b.decode("utf-8")
        def reset(self): pass
        def getstate(self):
            t = holder.get("t")
            if t is not None and self.armed:
                self.armed = False
                try:
                    t.detach()
                    emit("inner detach ok")
                except Exception as exc:
                    emit("inner detach raised", type(exc).__name__)
            return (b"", 0)
        def setstate(self, s): pass
    n = register("evil-gs8", dec_cls=D)
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding=n)
    holder["t"] = t
    t.read(1)
    try:
        emit("tell ->", t.tell())
    except Exception as exc:
        emit("tell raised", type(exc).__name__)
    emit("survived")
""")

case("codec-setstate-raises-on-seek", "codec", CODEC_PRELUDE + """
    class D:
        def __init__(self, errors="strict"): pass
        def decode(self, b, final=False): return b.decode("utf-8")
        def reset(self): pass
        def getstate(self): return (b"", 0)
        def setstate(self, s): raise RuntimeError("boom in setstate")
    n = register("evil-ss1", dec_cls=D)
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding=n)
    t.read(1)
    ck = t.tell()
    t.read()
    try:
        emit("seek ->", t.seek(ck))
    except Exception as exc:
        emit("seek raised", type(exc).__name__)
    emit("survived")
""")

case("codec-setstate-detaches-on-seek", "codec", CODEC_PRELUDE + """
    holder = {}
    class D:
        def __init__(self, errors="strict"): self.armed = True
        def decode(self, b, final=False): return b.decode("utf-8")
        def reset(self): pass
        def getstate(self): return (b"", 0)
        def setstate(self, s):
            t = holder.get("t")
            if t is not None and self.armed:
                self.armed = False
                try:
                    t.detach()
                    emit("inner detach ok")
                except Exception as exc:
                    emit("inner detach raised", type(exc).__name__)
    n = register("evil-ss2", dec_cls=D)
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding=n)
    holder["t"] = t
    t.read(1)
    ck = t.tell()
    t.read()
    try:
        emit("seek ->", t.seek(ck))
    except Exception as exc:
        emit("seek raised", type(exc).__name__)
    emit("survived")
""")

case("codec-encoder-returns-nonbytes", "codec", CODEC_PRELUDE + """
    class E:
        def __init__(self, errors="strict"): pass
        def encode(self, s, final=False): return "a str, not bytes"
        def reset(self): pass
        def getstate(self): return 0
        def setstate(self, s): pass
    n = register("evil-e1", enc_cls=E)
    t = io.TextIOWrapper(io.BytesIO(), encoding=n)
    t.write("abc")
    t.flush()
    emit("survived")
""")

case("codec-encoder-raises", "codec", CODEC_PRELUDE + """
    class E:
        def __init__(self, errors="strict"): pass
        def encode(self, s, final=False): raise RuntimeError("boom in encode")
        def reset(self): pass
        def getstate(self): return 0
        def setstate(self, s): pass
    n = register("evil-e2", enc_cls=E)
    t = io.TextIOWrapper(io.BytesIO(), encoding=n)
    try:
        t.write("abc")
        t.flush()
    except Exception as exc:
        emit("write raised", type(exc).__name__)
    emit("survived")
""")

case("codec-encoder-detaches", "codec", CODEC_PRELUDE + """
    holder = {}
    class E:
        def __init__(self, errors="strict"): self.armed = True
        def encode(self, s, final=False):
            t = holder.get("t")
            if t is not None and self.armed:
                self.armed = False
                try:
                    t.detach()
                    emit("inner detach ok")
                except Exception as exc:
                    emit("inner detach raised", type(exc).__name__)
            return s.encode("utf-8")
        def reset(self): pass
        def getstate(self): return 0
        def setstate(self, s): pass
    n = register("evil-e3", enc_cls=E)
    t = io.TextIOWrapper(io.BytesIO(), encoding=n)
    holder["t"] = t
    try:
        t.write("abc")
        t.flush()
    except Exception as exc:
        emit("write raised", type(exc).__name__)
    emit("survived")
""")

case("codec-encoder-getstate-lies", "codec", CODEC_PRELUDE + """
    class E:
        def __init__(self, errors="strict"): pass
        def encode(self, s, final=False): return s.encode("utf-8")
        def reset(self): pass
        def getstate(self): return "not an int"
        def setstate(self, s): pass
    n = register("evil-e4", enc_cls=E)
    t = io.TextIOWrapper(io.BytesIO(), encoding=n)
    t.write("abc")
    try:
        emit("tell ->", t.tell())
    except Exception as exc:
        emit("tell raised", type(exc).__name__)
""")

case("codec-nl-decoder-nonstr", "codec", """
    class D:
        def decode(self, b, final=False): return b"bytes"
        def reset(self): pass
        def getstate(self): return (b"", 0)
        def setstate(self, s): pass
    nl = io.IncrementalNewlineDecoder(D(), True)
    emit("decode ->", nl.decode(b"abc"))
""")

case("codec-nl-decoder-raises", "codec", """
    class D:
        def decode(self, b, final=False): raise RuntimeError("boom")
        def reset(self): pass
        def getstate(self): return (b"", 0)
        def setstate(self, s): pass
    nl = io.IncrementalNewlineDecoder(D(), True)
    emit("decode ->", nl.decode(b"abc"))
""")

case("codec-nl-setstate-lies", "codec", """
    nl = io.IncrementalNewlineDecoder(None, True)
    for st in ("nope", (b"", "x"), (b"", -1), (b"", 1 << 100), (1, 2, 3), ()):
        try:
            nl.setstate(st)
            emit("setstate", repr(st), "-> ok, getstate", repr(nl.getstate()))
        except Exception as exc:
            emit("setstate", repr(st), "raised", type(exc).__name__)
""")

case("codec-nl-decoder-none-translate", "codec", """
    nl = io.IncrementalNewlineDecoder(None, True)
    emit("decode ->", nl.decode("a\\r\\nb\\rc\\nd"))
    emit("newlines ->", nl.newlines)
    nl2 = io.IncrementalNewlineDecoder(None, False)
    emit("decode2 ->", nl2.decode("a\\r\\nb\\rc\\nd"))
""")

case("codec-nl-decoder-bad-decoder-type", "codec", """
    nl = io.IncrementalNewlineDecoder(42, True)
    try:
        emit("decode ->", nl.decode(b"abc"))
    except Exception as exc:
        emit("decode raised", type(exc).__name__)
""")

case("codec-textio-newline-invalid", "codec", """
    for nlv in ("\\r\\n\\r", "x", "\\n\\n", 42):
        try:
            t = io.TextIOWrapper(io.BytesIO(b"abc"), encoding="utf-8", newline=nlv)
            emit("newline", repr(nlv), "-> ok")
        except Exception as exc:
            emit("newline", repr(nlv), "raised", type(exc).__name__)
""")

case("codec-textio-surrogates", "codec", """
    t = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="surrogatepass")
    t.write("\\udc80\\ud800")
    t.flush()
    emit("value ->", t.buffer.getvalue())
    t2 = io.TextIOWrapper(io.BytesIO(b"\\xed\\xb2\\x80"), encoding="utf-8", errors="surrogatepass")
    emit("read ->", ascii(t2.read()))
""")

case("codec-textio-embedded-nul", "codec", """
    t = io.TextIOWrapper(io.BytesIO(b"a\\x00b\\nc\\x00"), encoding="utf-8")
    emit("read ->", ascii(t.read()))
    emit("lines ->", ascii(io.TextIOWrapper(io.BytesIO(b"a\\x00b\\nc"), encoding='utf-8').readlines()))
""")

case("codec-textio-tell-seek-roundtrip", "codec", """
    data = "line1\\nline2\\u00e9\\nline3\\n".encode("utf-8")
    t = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")
    marks = []
    while True:
        m = t.tell()
        c = t.read(1)
        if not c: break
        marks.append((m, c))
    for m, c in marks:
        t.seek(m)
        got = t.read(1)
        if got != c:
            emit("MISMATCH at", m, ascii(c), ascii(got))
    emit("roundtrip checked", len(marks))
""")

case("codec-textio-tell-cookie-tamper", "codec", """
    data = ("x" * 200).encode("utf-8")
    t = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")
    t.read(5)
    ck = t.tell()
    for delta in (1 << 70, -(1 << 70), (1 << 63), -1, 1 << 100):
        try:
            emit("seek", delta, "->", t.seek(ck + delta))
        except Exception as exc:
            emit("seek", delta, "raised", type(exc).__name__)
    emit("survived")
""")

case("codec-textio-seek-forged-cookie", "codec", """
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding="utf-8")
    # Forge a cookie whose dec_flags / bytes_to_feed / chars_to_skip fields lie.
    for raw in (0xFFFFFFFF, 1 << 64, (1 << 64) | (0xFF << 96), (1 << 128) - 1):
        try:
            emit("seek", hex(raw), "->", t.seek(raw))
            emit("  read ->", ascii(t.read(2)))
        except Exception as exc:
            emit("seek", hex(raw), "raised", type(exc).__name__)
    emit("survived")
""")

# ---------------------------------------------------------------- group 4:
# BOUNDARY SIZES.
# ---------------------------------------------------------------------------

case("bound-bytesio-read-sizes", "bounds", """
    b = io.BytesIO(b"abcdef")
    for n in (-1, -2, -(1 << 63), 0, sys.maxsize, 1 << 100):
        b.seek(0)
        try:
            emit("read", n, "->", len(b.read(n)))
        except Exception as exc:
            emit("read", n, "raised", type(exc).__name__)
""")

case("bound-bytesio-readline-sizes", "bounds", """
    b = io.BytesIO(b"ab\\ncdef")
    for n in (-1, -2, 0, sys.maxsize, 1 << 100):
        b.seek(0)
        try:
            emit("readline", n, "->", b.readline(n))
        except Exception as exc:
            emit("readline", n, "raised", type(exc).__name__)
""")

case("bound-bytesio-readlines-hint", "bounds", """
    b = io.BytesIO(b"a\\nb\\nc\\n")
    for n in (-1, 0, sys.maxsize, 1 << 100, -(1 << 100)):
        b.seek(0)
        try:
            emit("readlines", n, "->", b.readlines(n))
        except Exception as exc:
            emit("readlines", n, "raised", type(exc).__name__)
""")

case("bound-bytesio-truncate", "bounds", """
    b = io.BytesIO(b"abcdef")
    for n in (-1, 0, 3, 100, sys.maxsize):
        try:
            r = b.truncate(n)
            emit("truncate", n, "->", r, len(b.getvalue()))
        except Exception as exc:
            emit("truncate", n, "raised", type(exc).__name__)
""")

case("bound-stringio-sizes", "bounds", """
    s = io.StringIO("abcdef")
    for n in (-1, -2, 0, sys.maxsize, 1 << 100):
        s.seek(0)
        try:
            emit("read", n, "->", len(s.read(n)))
        except Exception as exc:
            emit("read", n, "raised", type(exc).__name__)
    for n in (-1, 0, 3, sys.maxsize):
        try:
            emit("truncate", n, "->", s.truncate(n))
        except Exception as exc:
            emit("truncate", n, "raised", type(exc).__name__)
""")

case("bound-stringio-seek", "bounds", """
    s = io.StringIO("abcdef")
    for pos, whence in ((-1, 0), (1 << 100, 0), (1, 1), (0, 1), (0, 2), (1, 2), (0, 3), (0, -1)):
        try:
            emit("seek", pos, whence, "->", s.seek(pos, whence))
        except Exception as exc:
            emit("seek", pos, whence, "raised", type(exc).__name__)
""")

case("bound-buffered-read-sizes", "bounds", """
    for n in (-1, -2, 0, sys.maxsize, 1 << 100):
        br = io.BufferedReader(io.BytesIO(b"abcdef"), buffer_size=4)
        try:
            emit("read", n, "->", br.read(n))
        except Exception as exc:
            emit("read", n, "raised", type(exc).__name__)
""")

case("bound-buffered-peek-sizes", "bounds", """
    for n in (-1, 0, 1 << 100, -(1 << 100)):
        br = io.BufferedReader(io.BytesIO(b"abcdef"), buffer_size=4)
        try:
            emit("peek", n, "->", br.peek(n))
        except Exception as exc:
            emit("peek", n, "raised", type(exc).__name__)
""")

case("bound-buffered-read1-sizes", "bounds", """
    for n in (-1, -2, 0, 1 << 100):
        br = io.BufferedReader(io.BytesIO(b"abcdef"), buffer_size=4)
        try:
            emit("read1", n, "->", br.read1(n))
        except Exception as exc:
            emit("read1", n, "raised", type(exc).__name__)
""")

case("bound-buffered-seek-whence", "bounds", """
    br = io.BufferedRandom(io.BytesIO(b"abcdef"), buffer_size=4)
    for pos, whence in ((0, 3), (0, -1), (0, 1 << 100), (-100, 0), (1 << 62, 0)):
        try:
            emit("seek", pos, whence, "->", br.seek(pos, whence))
        except Exception as exc:
            emit("seek", pos, whence, "raised", type(exc).__name__)
""")

case("bound-buffered-truncate", "bounds", """
    br = io.BufferedRandom(io.BytesIO(b"abcdef"), buffer_size=4)
    for n in (-1, 0, 3, sys.maxsize):
        try:
            emit("truncate", n, "->", br.truncate(n))
        except Exception as exc:
            emit("truncate", n, "raised", type(exc).__name__)
""")

case("bound-textio-read-sizes", "bounds", """
    for n in (-1, -2, 0, sys.maxsize, 1 << 100):
        t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding="utf-8")
        try:
            emit("read", n, "->", t.read(n))
        except Exception as exc:
            emit("read", n, "raised", type(exc).__name__)
""")

case("bound-textio-readline-sizes", "bounds", """
    for n in (-1, -2, 0, 1 << 100):
        t = io.TextIOWrapper(io.BytesIO(b"ab\\ncdef"), encoding="utf-8")
        try:
            emit("readline", n, "->", ascii(t.readline(n)))
        except Exception as exc:
            emit("readline", n, "raised", type(exc).__name__)
""")

case("bound-textio-truncate-seek", "bounds", """
    t = io.TextIOWrapper(io.BytesIO(b"abcdef"), encoding="utf-8")
    for n in (-1, 0, 3):
        try:
            emit("truncate", n, "->", t.truncate(n))
        except Exception as exc:
            emit("truncate", n, "raised", type(exc).__name__)
    for pos, whence in ((-1, 0), (1, 1), (1, 2), (0, 3)):
        try:
            emit("seek", pos, whence, "->", t.seek(pos, whence))
        except Exception as exc:
            emit("seek", pos, whence, "raised", type(exc).__name__)
""")

case("bound-fileio-read-sizes", "bounds", """
    p = tmpfile(b"abcdef")
    f = io.FileIO(p, "r")
    for n in (-1, -2, 0, 1 << 100):
        f.seek(0)
        try:
            emit("read", n, "->", f.read(n))
        except Exception as exc:
            emit("read", n, "raised", type(exc).__name__)
    f.close()
    os.unlink(p)
""")

case("bound-open-buffering", "bounds", """
    p = tmpfile(b"abcdef")
    for buf in (-1, 0, 1, 2, 1 << 100, -(1 << 100)):
        try:
            f = io.open(p, "rb", buffering=buf)
            emit("buffering", buf, "->", type(f).__name__)
            f.close()
        except Exception as exc:
            emit("buffering", buf, "raised", type(exc).__name__)
    os.unlink(p)
""")

case("bound-open-bad-modes", "bounds", """
    p = tmpfile(b"abcdef")
    for mode in ("", "rw", "rbt", "xw", "U", "rb+t", "a+b+", "r" * 100, "rr"):
        try:
            f = io.open(p, mode)
            emit("mode", repr(mode), "->", type(f).__name__)
            f.close()
        except Exception as exc:
            emit("mode", repr(mode), "raised", type(exc).__name__, str(exc))
    os.unlink(p)
""")

case("bound-open-text-with-buffering-0", "bounds", """
    p = tmpfile(b"abcdef")
    try:
        f = io.open(p, "r", buffering=0)
        emit("-> ok")
    except Exception as exc:
        emit("raised", type(exc).__name__, str(exc))
    os.unlink(p)
""")

# ---------------------------------------------------------------- group 5:
# LIVE MEMORYVIEW vs RESIZE on BytesIO.
# ---------------------------------------------------------------------------

case("mv-getbuffer-then-write", "memoryview", """
    b = io.BytesIO(b"abcdef")
    mv = b.getbuffer()
    try:
        b.write(b"xyz")
        emit("write -> ok")
    except Exception as exc:
        emit("write raised", type(exc).__name__)
    emit("mv ->", bytes(mv))
""")

case("mv-getbuffer-then-truncate", "memoryview", """
    b = io.BytesIO(b"abcdef")
    mv = b.getbuffer()
    try:
        b.truncate(2)
        emit("truncate -> ok")
    except Exception as exc:
        emit("truncate raised", type(exc).__name__)
    emit("mv ->", bytes(mv))
""")

case("mv-getbuffer-then-seek-write", "memoryview", """
    b = io.BytesIO(b"abcdef")
    mv = b.getbuffer()
    try:
        b.seek(100)
        b.write(b"z")
        emit("seek+write -> ok")
    except Exception as exc:
        emit("seek+write raised", type(exc).__name__)
    emit("mv ->", bytes(mv))
""")

case("mv-getbuffer-then-close", "memoryview", """
    b = io.BytesIO(b"abcdef")
    mv = b.getbuffer()
    try:
        b.close()
        emit("close -> ok")
    except Exception as exc:
        emit("close raised", type(exc).__name__)
    emit("mv ->", bytes(mv))
""")

case("mv-getbuffer-then-setstate", "memoryview", """
    b = io.BytesIO(b"abcdef")
    mv = b.getbuffer()
    try:
        b.__setstate__((b"z", 0, None))
        emit("setstate -> ok")
    except Exception as exc:
        emit("setstate raised", type(exc).__name__)
    emit("mv ->", bytes(mv))
""")

case("mv-getbuffer-then-writelines", "memoryview", """
    b = io.BytesIO(b"abcdef")
    mv = b.getbuffer()
    try:
        b.writelines([b"xyz" * 100])
        emit("writelines -> ok")
    except Exception as exc:
        emit("writelines raised", type(exc).__name__)
    emit("mv len ->", len(bytes(mv)))
""")

case("mv-getbuffer-then-readinto-self", "memoryview", """
    b = io.BytesIO(b"abcdef")
    mv = b.getbuffer()
    try:
        emit("readinto ->", b.readinto(mv))
    except Exception as exc:
        emit("readinto raised", type(exc).__name__)
    emit("mv ->", bytes(mv))
""")

case("mv-getbuffer-release-then-write", "memoryview", """
    b = io.BytesIO(b"abcdef")
    mv = b.getbuffer()
    mv.release()
    b.write(b"xyzxyzxyz")
    emit("value ->", b.getvalue())
""")

case("mv-getbuffer-twice", "memoryview", """
    b = io.BytesIO(b"abcdef")
    m1 = b.getbuffer()
    m2 = b.getbuffer()
    m1.release()
    try:
        b.write(b"z" * 100)
        emit("write -> ok")
    except Exception as exc:
        emit("write raised", type(exc).__name__)
    m2.release()
    b.write(b"z" * 100)
    emit("len ->", len(b.getvalue()))
""")

case("mv-getbuffer-then-init", "memoryview", """
    b = io.BytesIO(b"abcdef")
    mv = b.getbuffer()
    try:
        b.__init__(b"zzzzzzzzzzzzzzzzzzzz")
        emit("re-init -> ok")
    except Exception as exc:
        emit("re-init raised", type(exc).__name__)
    emit("mv ->", bytes(mv))
""")

case("mv-getbuffer-write-through-buffered", "memoryview", """
    b = io.BytesIO(b"abcdef")
    mv = b.getbuffer()
    w = io.BufferedWriter(b, buffer_size=4)
    try:
        w.write(b"z" * 64)
        w.flush()
        emit("buffered write -> ok")
    except Exception as exc:
        emit("buffered write raised", type(exc).__name__)
    emit("mv ->", bytes(mv))
""")

case("mv-bytesio-mv-of-mv", "memoryview", """
    b = io.BytesIO(b"abcdef")
    mv = b.getbuffer()
    mv2 = memoryview(mv)
    del mv
    try:
        b.write(b"z" * 100)
        emit("write -> ok")
    except Exception as exc:
        emit("write raised", type(exc).__name__)
    emit("mv2 ->", bytes(mv2))
""")

# ---------------------------------------------------------------- group 6:
# TEARDOWN.
# ---------------------------------------------------------------------------

case("tear-close-raises", "teardown", """
    class Raw(io.RawIOBase):
        def readable(self): return True
        def close(self): raise RuntimeError("boom in raw close")
    br = io.BufferedReader(Raw())
    try:
        br.close()
    except Exception as exc:
        emit("close raised", type(exc).__name__)
    emit("closed ->", br.closed)
    try:
        br.close()
        emit("second close ok")
    except Exception as exc:
        emit("second close raised", type(exc).__name__)
""")

case("tear-flush-raises-on-close", "teardown", """
    class Evil(io.BufferedWriter):
        def flush(self):
            raise RuntimeError("boom in flush")
    e = Evil(io.BytesIO())
    try:
        e.close()
    except Exception as exc:
        emit("close raised", type(exc).__name__)
    emit("closed ->", e.closed)
""")

case("tear-flush-and-close-both-raise", "teardown", """
    class Raw(io.RawIOBase):
        def writable(self): return True
        def write(self, b): raise RuntimeError("boom in write")
        def close(self): raise ValueError("boom in close")
    w = io.BufferedWriter(Raw(), buffer_size=1024)
    w.write(b"abc")
    try:
        w.close()
    except BaseException as exc:
        emit("close raised", type(exc).__name__, "ctx", type(exc.__context__).__name__)
    emit("closed ->", w.closed)
""")

case("tear-detach-then-use", "teardown", """
    br = io.BufferedReader(io.BytesIO(b"abc"))
    br.detach()
    for meth, args in (("read", ()), ("close", ()), ("detach", ()), ("tell", ()),
                       ("readable", ()), ("fileno", ()), ("seek", (0,)), ("peek", ())):
        try:
            emit(meth, "->", repr(getattr(br, meth)(*args)))
        except Exception as exc:
            emit(meth, "raised", type(exc).__name__, str(exc))
""")

case("tear-textio-detach-then-use", "teardown", """
    t = io.TextIOWrapper(io.BytesIO(b"abc"), encoding="utf-8")
    t.detach()
    for meth, args in (("read", ()), ("close", ()), ("detach", ()), ("tell", ()),
                       ("readable", ()), ("fileno", ()), ("seek", (0,)), ("write", ("x",))):
        try:
            emit(meth, "->", repr(getattr(t, meth)(*args)))
        except Exception as exc:
            emit(meth, "raised", type(exc).__name__, str(exc))
""")

case("tear-closed-reuse-bytesio", "teardown", """
    b = io.BytesIO(b"abc")
    b.close()
    for meth, args in (("read", ()), ("write", (b"x",)), ("seek", (0,)), ("tell", ()),
                       ("truncate", ()), ("getvalue", ()), ("getbuffer", ()),
                       ("readinto", (bytearray(3),)), ("readline", ()), ("__init__", (b"z",)),
                       ("__setstate__", ((b"z", 0, None),))):
        try:
            emit(meth, "->", repr(getattr(b, meth)(*args)))
        except Exception as exc:
            emit(meth, "raised", type(exc).__name__)
""")

case("tear-closed-reuse-stringio", "teardown", """
    s = io.StringIO("abc")
    s.close()
    for meth, args in (("read", ()), ("write", ("x",)), ("seek", (0,)), ("tell", ()),
                       ("truncate", ()), ("getvalue", ()), ("readline", ()),
                       ("__init__", ("z",)), ("__setstate__", (("z", "\\n", 0, None),))):
        try:
            emit(meth, "->", repr(getattr(s, meth)(*args)))
        except Exception as exc:
            emit(meth, "raised", type(exc).__name__)
""")

case("tear-double-close-textio", "teardown", """
    t = io.TextIOWrapper(io.BytesIO(b"abc"), encoding="utf-8")
    t.close()
    t.close()
    emit("closed ->", t.closed)
    try:
        emit("read ->", t.read())
    except Exception as exc:
        emit("read raised", type(exc).__name__)
""")

case("tear-setstate-lies-bytesio", "teardown", """
    b = io.BytesIO(b"abc")
    for st in ((b"z", -1, None), (b"z", 1 << 100, None), ("str", 0, None),
               (b"z", 0), (), (b"z", 0, 42), (b"z", "x", None)):
        try:
            b.__setstate__(st)
            emit("setstate", repr(st), "-> ok tell", b.tell())
        except Exception as exc:
            emit("setstate", repr(st), "raised", type(exc).__name__)
""")

case("tear-setstate-lies-stringio", "teardown", """
    s = io.StringIO("abc")
    for st in (("z", "\\n", -1, None), ("z", "\\n", 1 << 100, None),
               (b"z", "\\n", 0, None), ("z", 42, 0, None), (), ("z",),
               ("z", "\\n", 0, 42)):
        try:
            s.__setstate__(st)
            emit("setstate", repr(st), "-> ok tell", s.tell())
        except Exception as exc:
            emit("setstate", repr(st), "raised", type(exc).__name__)
""")

case("tear-iobase-del-closed-property-lies", "teardown", """
    class Evil(io.RawIOBase):
        @property
        def closed(self):
            raise RuntimeError("boom in closed")
    e = Evil()
    try:
        e.close()
    except Exception as exc:
        emit("close raised", type(exc).__name__)
    del e
    emit("survived del")
""")

case("tear-iobase-closed-property-lies-2", "teardown", """
    class Evil(io.RawIOBase):
        n = 0
        @property
        def closed(self):
            type(self).n += 1
            return type(self).n > 3
    e = Evil()
    e.close()
    emit("n ->", Evil.n)
    emit("survived")
""")

case("tear-del-resurrect", "teardown", """
    import gc
    keep = []
    class Evil(io.BufferedWriter):
        def __del__(self):
            keep.append(self)
            emit("resurrected")
            try:
                super().__del__()
            except Exception:
                pass
    e = Evil(io.BytesIO())
    e.write(b"abc")
    del e
    gc.collect()
    emit("keep ->", len(keep))
    if keep:
        try:
            emit("post-resurrect write ->", keep[0].write(b"z"))
        except Exception as exc:
            emit("post-resurrect write raised", type(exc).__name__)
    emit("survived")
""")

case("tear-self-referential-buffer", "teardown", """
    b = io.BytesIO()
    w = io.BufferedWriter(b)
    t = io.TextIOWrapper(w, encoding="utf-8")
    # a wrapper wrapping itself: TextIOWrapper over its own buffer chain
    try:
        t2 = io.TextIOWrapper(t, encoding="utf-8")
        emit("nested -> ok")
        t2.write("x")
        t2.flush()
    except Exception as exc:
        emit("nested raised", type(exc).__name__)
    emit("survived")
""")

case("tear-rwpair-same-object", "teardown", """
    b = io.BytesIO(b"abc")
    p = io.BufferedRWPair(b, b)
    p.write(b"xyz")
    try:
        emit("read ->", p.read(3))
    except Exception as exc:
        emit("read raised", type(exc).__name__)
    p.close()
    emit("survived")
""")

case("tear-buffered-wraps-self", "teardown", """
    b = io.BufferedRandom(io.BytesIO(b"abc"))
    try:
        b2 = io.BufferedRandom(b)
        b2.write(b"z")
        b2.flush()
        emit("nested ok")
    except Exception as exc:
        emit("nested raised", type(exc).__name__)
    emit("survived")
""")

case("tear-open-closefd-false-int", "teardown", """
    p = tmpfile(b"abc")
    fd = os.open(p, os.O_RDONLY)
    try:
        f = io.open(fd, "rb", closefd=False)
        f.close()
        emit("closefd=False -> ok")
    except Exception as exc:
        emit("raised", type(exc).__name__)
    try:
        f2 = io.open(fd, "rb", closefd=True)
        f2.close()
        emit("closefd=True -> ok")
    except Exception as exc:
        emit("raised2", type(exc).__name__)
    os.unlink(p)
""")

case("tear-fileio-closefd-false-requires-fd", "teardown", """
    p = tmpfile(b"abc")
    try:
        f = io.FileIO(p, "r", closefd=False)
        emit("-> ok")
    except Exception as exc:
        emit("raised", type(exc).__name__, str(exc))
    os.unlink(p)
""")

case("tear-fileio-reinit", "teardown", """
    p = tmpfile(b"abcdef")
    f = io.FileIO(p, "r")
    fd1 = f.fileno()
    f.__init__(p, "r")
    fd2 = f.fileno()
    emit("fd changed ->", fd1 != fd2)
    f.close()
    try:
        os.close(fd1)
        emit("fd1 still open -> LEAK")
    except OSError:
        emit("fd1 closed")
    os.unlink(p)
""")

case("tear-bufferedwriter-unwritable-raw", "teardown", """
    class Raw(io.RawIOBase):
        def writable(self): return False
    try:
        w = io.BufferedWriter(Raw())
        emit("-> ok")
    except Exception as exc:
        emit("raised", type(exc).__name__)
""")

case("tear-mode-mutating-writable", "teardown", """
    class Raw(io.RawIOBase):
        n = 0
        def writable(self):
            type(self).n += 1
            return type(self).n <= 1
        def readable(self): return True
        def write(self, b): return len(b)
        def readinto(self, b): return 0
    w = io.BufferedWriter(Raw(), buffer_size=4)
    try:
        w.write(b"abcdefgh")
        w.flush()
        emit("write -> ok")
    except Exception as exc:
        emit("write raised", type(exc).__name__)
    emit("survived")
""")

case("tear-seekable-mutating", "teardown", """
    class Raw(io.RawIOBase):
        n = 0
        def readable(self): return True
        def seekable(self):
            type(self).n += 1
            return type(self).n <= 1
        def seek(self, p, w=0): return 0
        def tell(self): return 0
        def readinto(self, b): return 0
    br = io.BufferedReader(Raw(), buffer_size=4)
    for i in range(4):
        try:
            emit("seek", i, "->", br.seek(0))
        except Exception as exc:
            emit("seek", i, "raised", type(exc).__name__)
    emit("survived")
""")

case("tear-textio-buffer-goes-away", "teardown", """
    import gc
    b = io.BytesIO(b"abcdef")
    t = io.TextIOWrapper(b, encoding="utf-8")
    del b
    gc.collect()
    emit("read ->", t.read())
    emit("survived")
""")

case("tear-iobase-readline-lying-peek", "teardown", """
    class Evil(io.RawIOBase):
        def readable(self): return True
        def peek(self, n=0): return "not bytes"
        def read(self, n=-1): return b"ab\\ncd"
    e = Evil()
    try:
        emit("readline ->", e.readline())
    except Exception as exc:
        emit("readline raised", type(exc).__name__)
""")

case("tear-iobase-readline-read-lies", "teardown", """
    class Evil(io.RawIOBase):
        def readable(self): return True
        def read(self, n=-1): return 42
    e = Evil()
    try:
        emit("readline ->", e.readline())
    except Exception as exc:
        emit("readline raised", type(exc).__name__)
""")

case("tear-iobase-readlines-lying-iter", "teardown", """
    class Evil(io.RawIOBase):
        def readable(self): return True
        def __iter__(self): return self
        def __next__(self): raise RuntimeError("boom in next")
    e = Evil()
    try:
        emit("readlines ->", e.readlines())
    except Exception as exc:
        emit("readlines raised", type(exc).__name__)
""")

case("tear-iobase-writelines-closes", "teardown", """
    class Evil(io.RawIOBase):
        def writable(self): return True
        def write(self, b):
            self.close()
            return len(b)
    e = Evil()
    try:
        e.writelines([b"a", b"b", b"c"])
        emit("writelines -> ok")
    except Exception as exc:
        emit("writelines raised", type(exc).__name__)
    emit("survived")
""")

case("tear-nested-context-managers", "teardown", """
    class Evil(io.BytesIO):
        def __exit__(self, *a):
            raise RuntimeError("boom in exit")
    try:
        with Evil(b"abc") as f:
            pass
    except Exception as exc:
        emit("exit raised", type(exc).__name__)
    b = io.BytesIO(b"abc")
    b.close()
    try:
        with b:
            emit("entered a closed file")
    except Exception as exc:
        emit("enter raised", type(exc).__name__)
""")

case("tear-unsupported-operation-identity", "teardown", """
    emit("UnsupportedOperation bases ->",
         sorted(c.__name__ for c in io.UnsupportedOperation.__mro__))
    b = io.BytesIO(b"abc")
    try:
        b.fileno()
    except Exception as exc:
        emit("fileno raised", type(exc).__name__)
""")

case("tear-abc-registration", "teardown", """
    emit("BytesIO is BufferedIOBase ->", isinstance(io.BytesIO(), io.BufferedIOBase))
    emit("StringIO is TextIOBase ->", isinstance(io.StringIO(), io.TextIOBase))
    emit("BufferedReader is BufferedIOBase ->",
         isinstance(io.BufferedReader(io.BytesIO()), io.BufferedIOBase))
    emit("IOBase subclasshook ->", issubclass(io.BytesIO, io.IOBase))
""")


# --------------------------------------------------------------------------
# Runner.
# --------------------------------------------------------------------------

EXC_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning|Exit|Interrupt|"
                    r"Iteration|Operation|Overflow|Found|Empty|Full))\b", re.M)


def last_exception(stderr: str) -> str:
    """Best-effort extraction of the final exception type name from a traceback."""
    lines = [ln for ln in stderr.strip().splitlines() if ln and not ln.startswith((" ", "\t"))]
    for ln in reversed(lines):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)(:|$)", ln)
        if m and m.group(1) not in ("Traceback", "During", "The"):
            return m.group(1).rsplit(".", 1)[-1]
    return ""


def signal_name(rc: int) -> str:
    table = {-11: "SIGSEGV", -6: "SIGABRT", -4: "SIGILL", -8: "SIGFPE",
             -9: "SIGKILL", -7: "SIGBUS"}
    return table.get(rc, "SIG%d" % -rc)


class Result:
    def __init__(self, rc, out, err, timed_out=False):
        self.rc, self.out, self.err, self.timed_out = rc, out, err, timed_out

    @property
    def crashed(self) -> bool:
        return self.rc is not None and self.rc < 0

    @property
    def fatal(self) -> bool:
        return "Fatal Python error" in self.err

    @property
    def systemerror(self) -> bool:
        return "SystemError" in self.err

    def summary(self) -> str:
        if self.timed_out:
            return "TIMEOUT"
        if self.crashed:
            return "%s (rc=%d)" % (signal_name(self.rc), self.rc)
        if self.rc == 0:
            return "ok"
        return "%s (rc=%d)" % (last_exception(self.err) or "exit", self.rc)


def run_one(interp: str, body: str, importline: str, timeout: float) -> Result:
    src = PRELUDE.format(importline=importline) + "\n" + body
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            p = subprocess.run([interp, path], capture_output=True, text=True,
                               timeout=timeout, env=env, errors="replace")
        except subprocess.TimeoutExpired as e:
            return Result(None, (e.stdout or b"").decode("utf-8", "replace")
                          if isinstance(e.stdout, bytes) else (e.stdout or ""),
                          (e.stderr or b"").decode("utf-8", "replace")
                          if isinstance(e.stderr, bytes) else (e.stderr or ""),
                          timed_out=True)
        return Result(p.returncode, p.stdout, p.stderr)
    finally:
        os.unlink(path)


def classify(c: Result, y: Result) -> str:
    if c.timed_out and y.timed_out:
        return "BOTH_TIMEOUT"
    if c.timed_out:
        return "C_TIMEOUT"
    if y.timed_out:
        return "PY_TIMEOUT"
    if c.crashed and y.crashed:
        return "BOTH_CRASH"
    if c.crashed:
        return "C_CRASH"
    if y.crashed:
        return "PY_CRASH"
    # SystemError / fatal on one side only
    c_bad = c.systemerror or c.fatal
    y_bad = y.systemerror or y.fatal
    if c_bad and not y_bad:
        return "C_CONTRACT"
    if y_bad and not c_bad:
        return "PY_CONTRACT"
    if c.rc == 0 and y.rc == 0:
        return "AGREE" if c.out == y.out else "OUTPUT_DIFF"
    if c.rc != 0 and y.rc != 0:
        ce, ye = last_exception(c.err), last_exception(y.err)
        if ce == ye and c.out == y.out:
            return "AGREE"
        return "EXC_DIFF" if ce != ye else "OUTPUT_DIFF"
    return "EXC_DIFF"


ORDER = ["C_CRASH", "C_CONTRACT", "PY_CRASH", "PY_CONTRACT", "C_TIMEOUT", "PY_TIMEOUT",
         "EXC_DIFF", "OUTPUT_DIFF", "BOTH_CRASH", "BOTH_TIMEOUT", "AGREE"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--filter", default="")
    ap.add_argument("--group", default="")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    cases = [c for c in CASES
             if args.filter in c[0] and (not args.group or c[1] == args.group)]
    if args.list:
        for n, g, _ in cases:
            print("%-12s %s" % (g, n))
        return 0

    print("interpreter: %s" % args.python)
    print("cases: %d\n" % len(cases))

    tally: dict[str, list[str]] = {}
    rows = []
    for name, group, body in cases:
        verdicts = set()
        for _ in range(args.repeat):
            c = run_one(args.python, body, C_IMPORT, args.timeout)
            y = run_one(args.python, body, PY_IMPORT, args.timeout)
            verdicts.add(classify(c, y))
        v = sorted(verdicts, key=lambda k: ORDER.index(k) if k in ORDER else 99)[0]
        tally.setdefault(v, []).append(name)
        rows.append((v, group, name, c.summary(), y.summary()))
        if args.verbose or v not in ("AGREE",):
            print("[%-12s] %-14s %-40s  C=%-22s py=%s"
                  % (v, group, name, c.summary(), y.summary()))
            if args.verbose and v != "AGREE":
                for label, r in (("C ", c), ("py", y)):
                    tail = "\n".join(r.err.strip().splitlines()[-4:])
                    if tail:
                        print("      %s stderr: %s" % (label, tail.replace("\n", "\n              ")))
                    if r.out.strip():
                        print("      %s stdout: %s" % (label, r.out.strip().replace("\n", " | ")))

    print("\n--- tally ---")
    for k in ORDER:
        if k in tally:
            print("%-14s %3d   %s" % (k, len(tally[k]), ", ".join(tally[k][:6])
                                      + (" ..." if len(tally[k]) > 6 else "")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

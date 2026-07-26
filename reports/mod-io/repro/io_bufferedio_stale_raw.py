"""Modules/_io/bufferedio.c never re-validates self->raw after user Python runs.

FIVE reproduced NULL-receiver dispatch sites.  All have the same shape:

    <read/validate self->raw or CHECK_INITIALIZED>
    ... a call that runs ARBITRARY user Python (self.flush(), raw.write(),
        raw.read(), raw.readinto(), raw.seek()) ...
    PyObject_CallMethod{NoArgs,OneArg}(self->raw, ...)   <-- self->raw is now NULL

`_io__Buffered_detach_impl` (bufferedio.c:617) is the only _Buffered method that
takes NO ENTER_BUFFERED, and it dispatches the *Python-level* `flush`.  A
subclass whose `flush()` is a no-op therefore lets `detach()` complete --
`self->raw = NULL; self->ok = 0` -- from inside a callback any of the above
methods is holding the buffered lock across.

`PyObject_CallMethodNoArgs`/`OneArg` do NOT NULL-check their receiver: they go
straight to `_PyObject_GetMethodStackRef`, which reads `Py_TYPE(obj)->...`
=> SEGV on address 0x8.  (`PyObject_CallMethodObjArgs`, used by
`_buffered_raw_seek`:818, DOES check, and yields `SystemError: null argument to
internal routine` instead -- a C-contract violation rather than a crash.)

GUARDED TWIN: Modules/_io/textio.c:740 `buffer_access_safe()`, added by
db4b1948bc4 (gh-143008 / gh-142594, PR #145957, 2026-06-09) with the comment

    self->buffer can be detached (set to NULL) by any user code that is called
    leading to NULL pointer dereferences (see gh-143008, gh-142594).

That commit touched ONLY Modules/_io/textio.c.  bufferedio.c has the identical
shape and received none of the hardening.

Usage:  python io_bufferedio_stale_raw.py <site> <io|_pyio>
        python io_bufferedio_stale_raw.py --all --python /path/to/python [-n 5]
"""

import argparse
import subprocess
import sys

# site -> (C file:line, C function, driver source)
SITES = {
    "close:591": ("bufferedio.c:591", "_io__Buffered_close_impl", """
        class B(io.BufferedWriter):
            armed = True
            def flush(self):
                if self.armed:
                    self.armed = False
                    super().detach()
        B(io.BytesIO()).close()
    """),
    "raw_read:1640": ("bufferedio.c:1640", "_bufferedreader_raw_read", """
        class Raw(io.RawIOBase):
            def readable(self): return True
            def readinto(self, b):
                fire()
                b[0:1] = b"a"
                return 1
        b = mk(io.BufferedReader, Raw(), 4)
        b.read(64)
    """),
    "read_all:1748": ("bufferedio.c:1748", "_bufferedreader_read_all", """
        class Duck:                       # duck-typed: no RawIOBase.readall
            closed = False
            n = 0
            def readable(self): return True
            def writable(self): return False
            def seekable(self): return False
            def close(self): pass
            def flush(self): pass
            def read(self, *a):
                Duck.n += 1
                if Duck.n == 1:
                    fire()
                return b"abc" if Duck.n < 3 else b""
            def readinto(self, buf):
                d = self.read()
                buf[0:len(d)] = d
                return len(d)
        b = mk(io.BufferedReader, Duck(), 4)
        b.read()
    """),
    "raw_write:1996": ("bufferedio.c:1996", "_bufferedwriter_raw_write", """
        class Raw(io.RawIOBase):
            def writable(self): return True
            def write(self, b):
                fire()
                return 1                  # PARTIAL -> the flush loop iterates
        b = mk(io.BufferedWriter, Raw(), 4)
        b.write(b"0123456789abcdef")
    """),
    "truncate:1485": ("bufferedio.c:1485", "_io__Buffered_truncate_impl", """
        class Raw(io.RawIOBase):
            def readable(self): return False   # skip _buffered_raw_seek
            def writable(self): return True
            def seekable(self): return True
            def tell(self): return 0
            def seek(self, p, w=0): return 0
            def truncate(self, p=None): return 0
            def write(self, b):
                fire()
                return len(b)
        b = mk(io.BufferedWriter, Raw(), 64)
        b.write(b"012")
        b.truncate(1)
    """),
    "raw_tell:788": ("bufferedio.c:788", "_buffered_raw_tell", """
        class Raw(io.RawIOBase):
            def readable(self): return False
            def writable(self): return True
            def seekable(self): return True
            def tell(self): return 0
            def seek(self, p, w=0): return 0
            def write(self, b): return len(b)
            def truncate(self, p=None):
                fire()                    # detach from raw.truncate, AFTER :1485
                return 0
        b = mk(io.BufferedWriter, Raw(), 64)
        b.write(b"012")
        b.truncate(1)                     # crashes at :1489 -> _buffered_raw_tell:788
    """),
    # not a crash -- the contract-violation sibling, kept for completeness
    "raw_seek:818": ("bufferedio.c:818", "_buffered_raw_seek", """
        class Raw(io.RawIOBase):
            def readable(self): return True
            def writable(self): return True
            def seekable(self): return True
            def tell(self): return 0
            def seek(self, p, w=0): return 0
            def readinto(self, b): b[0:1] = b"a"; return 1
            def write(self, b):
                fire()
                return len(b)
        b = mk(io.BufferedRandom, Raw(), 64)
        b.write(b"012")
        b.seek(0)
    """),
}

HEADER = """\
import sys
{importline}

_state = {{}}

def mk(cls, raw, bufsize):
    class B(cls):
        def flush(self):        # keeps detach()'s _PyFile_Flush off the lock
            return None
    b = B(raw, buffer_size=bufsize)
    _state["b"] = b
    return b

def fire():
    if _state.get("fired") or "b" not in _state:
        return
    _state["fired"] = True
    try:
        _state["b"].detach()
        print("[detached]", file=sys.stderr)
    except BaseException as exc:
        print("[detach raised %s]" % type(exc).__name__, file=sys.stderr)
"""


def build(site, backend):
    importline = "import io" if backend == "io" else "import _pyio as io"
    import textwrap
    return HEADER.format(importline=importline) + textwrap.dedent(SITES[site][2])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site", nargs="?")
    ap.add_argument("backend", nargs="?", default="io")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("-n", type=int, default=3)
    args = ap.parse_args()

    if not args.all:
        if not args.site:
            for s, (loc, fn, _) in SITES.items():
                print("%-16s %-22s %s" % (s, loc, fn))
            return 0
        sys.stdout.write(build(args.site, args.backend))
        return 0

    print("%-16s %-22s %-34s %-14s %s"
          % ("site", "C source", "C function", "C (io)", "twin (_pyio)"))
    worst = 0
    for site, (loc, fn, _) in SITES.items():
        cells = []
        for backend in ("io", "_pyio"):
            rcs = []
            for _ in range(args.n):
                p = subprocess.run([args.python, "-c", build(site, backend)],
                                   capture_output=True, text=True, timeout=90)
                rcs.append(p.returncode)
            uniq = sorted(set(rcs))
            tag = {-11: "SIGSEGV", -6: "SIGABRT", 0: "ok"}.get(uniq[0], "exc")
            if backend == "io" and uniq[0] < 0:
                worst = 1
            cells.append("%s %d/%d" % (tag, rcs.count(uniq[0]), len(rcs)))
        print("%-16s %-22s %-34s %-14s %s" % (site, loc, fn, cells[0], cells[1]))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())

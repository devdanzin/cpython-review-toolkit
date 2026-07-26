"""Sweep: which _io._Buffered entry points dispatch through a NULL self->raw?

`_io__Buffered_detach_impl` (Modules/_io/bufferedio.c:617-629) is the only
_Buffered method that does NOT take ENTER_BUFFERED.  It calls
`_PyFile_Flush(self)`, which dispatches the *Python-level* `flush`; a subclass
that overrides `flush()` with a no-op therefore lets `detach()` run to
completion -- setting `self->raw = NULL`, `self->ok = 0` -- from inside a region
another _Buffered method is holding the buffered lock for.

Every method that reads `self->raw` after a point where user Python can run is
then dispatching on NULL.  This script drives each entry point with a raw whose
own method re-enters detach(), in its own subprocess, and reports the exit code.

Run:  python io_raw_null_sweep.py [--backend io|_pyio] [--case NAME]
      python io_raw_null_sweep.py --run-all --python /path/to/python
"""

import argparse
import os
import subprocess
import sys
import textwrap

HEADER = """\
import sys, faulthandler
faulthandler.enable()
{importline}

class NoFlush:
    # kills the reentrancy check: detach()'s _PyFile_Flush dispatches THIS,
    # which never re-takes the buffered lock.
    def flush(self):
        return None

def mk(kind, hook_name, buffer_size=8):
    \"\"\"Build a Buffered<kind> over a raw whose `hook_name` re-enters detach().\"\"\"
    holder = {{}}

    class Raw(io.RawIOBase):
        def readable(self): return True
        def writable(self): return True
        def seekable(self): return True
        def tell(self): return 0
        def readinto(self, b):
            self._hook("readinto")
            b[0:1] = b"a"
            return 1
        def read(self, n=-1):
            self._hook("read")
            return b"a"
        def readall(self):
            self._hook("readall")
            return b"a"
        def write(self, b):
            self._hook("write")
            return len(b)
        def seek(self, p, w=0):
            self._hook("seek")
            return 0
        def truncate(self, p=None):
            self._hook("truncate")
            return 0
        def _hook(self, who):
            if who != hook_name:
                return
            b = holder.get("b")
            if b is None or holder.get("fired"):
                return
            holder["fired"] = True
            try:
                b.detach()
                print("   [detached from raw.%s]" % who, file=sys.stderr)
            except BaseException as exc:
                print("   [detach in raw.%s raised %s]" % (who, type(exc).__name__),
                      file=sys.stderr)

    cls = {{"reader": io.BufferedReader,
           "writer": io.BufferedWriter,
           "random": io.BufferedRandom}}[kind]

    class B(NoFlush, cls):
        pass

    b = B(Raw(), buffer_size=buffer_size)
    holder["b"] = b
    return b
"""

CASES = {
    # name: (kind, raw-hook that fires detach, driver body)
    "close-after-flush": ("writer", None, """
        b = mk("writer", None)
        b.write(b"x")
        # detach directly from the Python flush the close() drives
        class B2(io.BufferedWriter):
            armed = True
            def flush(self):
                if self.armed:
                    self.armed = False
                    super().detach()
        b2 = B2(io.BytesIO())
        b2.close()
    """),
    "truncate-after-flush": ("random", "write", """
        b = mk("random", "write", buffer_size=4)
        b.write(b"0123456789")     # force a real raw write on the next flush
        print("truncate ->", b.truncate(2), file=sys.stderr)
    """),
    "readall-loop": ("reader", "read", """
        b = mk("reader", "read", buffer_size=4)
        print("read() ->", b.read(), file=sys.stderr)
    """),
    "read-generic-loop": ("reader", "readinto", """
        b = mk("reader", "readinto", buffer_size=4)
        print("read(64) ->", b.read(64), file=sys.stderr)
    """),
    "write-loop": ("writer", "write", """
        b = mk("writer", "write", buffer_size=4)
        print("write ->", b.write(b"0123456789abcdefghij"), file=sys.stderr)
        b.flush()
    """),
    "seek-after-flush": ("random", "write", """
        b = mk("random", "write", buffer_size=4)
        b.write(b"0123456789")
        print("seek ->", b.seek(0), file=sys.stderr)
    """),
    "peek-after-flush": ("random", "write", """
        b = mk("random", "write", buffer_size=4)
        b.write(b"0123456789")
        print("peek ->", b.peek(1), file=sys.stderr)
    """),
    "read1-after-flush": ("random", "write", """
        b = mk("random", "write", buffer_size=4)
        b.write(b"0123456789")
        print("read1 ->", b.read1(2), file=sys.stderr)
    """),
    "flush-after-rawwrite": ("writer", "write", """
        b = mk("writer", "write", buffer_size=4)
        b.write(b"0123456789")
        print("flush ->", b.flush(), file=sys.stderr)
    """),
    "readinto-after-flush": ("random", "write", """
        b = mk("random", "write", buffer_size=4)
        b.write(b"0123456789")
        print("readinto ->", b.readinto(bytearray(8)), file=sys.stderr)
    """),
    "tell-after-flush": ("random", "write", """
        b = mk("random", "write", buffer_size=4)
        b.write(b"0123456789")
        print("tell ->", b.tell(), file=sys.stderr)
    """),
    "readline-after-flush": ("random", "write", """
        b = mk("random", "write", buffer_size=4)
        b.write(b"0123456789")
        print("readline ->", b.readline(), file=sys.stderr)
    """),
    "iternext-after-flush": ("random", "write", """
        b = mk("random", "write", buffer_size=4)
        b.write(b"0123456789")
        print("next ->", next(iter(b)), file=sys.stderr)
    """),
}


def build(name, backend):
    _, _, body = CASES[name]
    importline = "import io" if backend == "io" else "import _pyio as io"
    return HEADER.format(importline=importline) + "\n" + textwrap.dedent(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="io", choices=["io", "_pyio"])
    ap.add_argument("--case", default="")
    ap.add_argument("--run-all", action="store_true")
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    if args.run_all:
        print("%-24s %-22s %s" % ("case", "C (import io)", "py (import _pyio)"))
        for name in CASES:
            outs = []
            for backend in ("io", "_pyio"):
                src = build(name, backend)
                p = subprocess.run([args.python, "-c", src], capture_output=True,
                                   text=True, timeout=60)
                tail = ""
                if p.returncode < 0:
                    tail = "SIG%d" % -p.returncode
                elif p.returncode:
                    last = [l for l in p.stderr.strip().splitlines()
                            if l and not l.startswith((" ", "\t"))]
                    tail = last[-1].split(":")[0] if last else "exit%d" % p.returncode
                else:
                    tail = "ok"
                outs.append("%-22s" % ("%s (rc=%d)" % (tail, p.returncode)))
            print("%-24s %s %s" % (name, outs[0], outs[1]))
        return 0

    if not args.case:
        for name in CASES:
            print(name)
        return 0
    sys.stdout.write(build(args.case, args.backend))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

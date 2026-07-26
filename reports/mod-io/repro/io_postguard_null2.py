"""Post-guard NULL probes, round 2: the remaining self->raw consumers.

Targets bufferedio.c:1485 (truncate), :1748 (read_all loop), :818 (raw_seek
reached from buffered_flush_and_rewind_unlocked:921), :1996 (raw_write loop).

Every scenario uses the same vehicle: a _Buffered SUBCLASS whose flush() is
inert, so detach()'s own internal _PyFile_Flush(self) succeeds instead of
tripping the "reentrant call inside" guard, plus a raw whose method detaches.

Usage: io_postguard_null2.py <scenario> <backend>
"""

import sys

scenario = sys.argv[1]
backend = sys.argv[2]

if backend == "_pyio":
    import _pyio as iomod
else:
    import io as iomod

holder = {}


class InertFlush:
    """Mixin: make flush() a no-op so a re-entrant detach() can complete."""

    def flush(self):
        pass


# --- S1: _io__Buffered_truncate_impl reads self->raw at bufferedio.c:1485 ----
# truncate() -> ENTER_BUFFERED -> buffered_flush_and_rewind_unlocked()
#            -> _bufferedwriter_raw_write -> raw.write() -> detach()
# then :1485 does PyObject_CallMethodOneArg(self->raw /* NULL */, truncate, pos)
if scenario == "truncate":

    class Raw(iomod.BytesIO):
        fired = False

        def write(self, b):
            cls = type(self)
            if not cls.fired:
                cls.fired = True
                holder["b"].detach()
            return super().write(b)

    class B(InertFlush, iomod.BufferedWriter):
        pass

    b = B(Raw())
    holder["b"] = b
    b.write(b"hello world")  # fill the write buffer
    print("before truncate", flush=True)
    b.truncate(4)
    print("survived: truncate() returned", flush=True)


# --- S2: _bufferedreader_read_all reads self->raw at bufferedio.c:1748 ------
# The raw must NOT have a `readall` attribute, or :1716 short-circuits the loop.
elif scenario == "readall_loop":

    class DuckRaw:  # deliberately NOT a RawIOBase: no readall attribute
        n = 0
        closed = False

        def readable(self):
            return True

        def writable(self):
            return False

        def seekable(self):
            return False

        def close(self):
            pass

        def read(self, size=-1):
            cls = type(self)
            cls.n += 1
            if cls.n == 1:
                holder["b"].detach()
                return b"chunk"
            return b""

    class B(InertFlush, iomod.BufferedReader):
        pass

    b = B(DuckRaw())
    holder["b"] = b
    print("before read", flush=True)
    b.read()
    print("survived: read() returned", flush=True)


# --- S3: _buffered_raw_seek reads self->raw at bufferedio.c:818 -------------
# reached from buffered_flush_and_rewind_unlocked:921, i.e. AFTER :912's
# writer-flush already ran raw.write() -> detach(). Needs readable+writable.
elif scenario == "flush_rewind_seek":

    class Raw(iomod.BytesIO):
        fired = False

        def write(self, b):
            cls = type(self)
            if not cls.fired:
                cls.fired = True
                holder["b"].detach()
            return super().write(b)

    class B(InertFlush, iomod.BufferedRandom):
        pass

    b = B(Raw())
    holder["b"] = b
    b.write(b"hello world")
    print("before read", flush=True)
    b.read(1)  # read path flushes+rewinds first
    print("survived: read() returned", flush=True)


# --- S4: _bufferedwriter_raw_write loop re-reads self->raw at :1996 ---------
# A short write forces a second loop iteration in
# _bufferedwriter_flush_unlocked:2039; the first raw.write() detaches.
elif scenario == "raw_write_loop":

    class Raw(iomod.RawIOBase):
        n = 0

        def writable(self):
            return True

        def write(self, b):
            cls = type(self)
            cls.n += 1
            if cls.n == 1:
                holder["b"].detach()
                return 1  # short write -> the flush loop iterates again
            return len(b)

    class B(InertFlush, iomod.BufferedWriter):
        pass

    b = B(Raw())
    holder["b"] = b
    b.write(b"hello world")
    print("before flush", flush=True)
    try:
        iomod.BufferedWriter.flush(b)  # bypass the inert override
    except Exception as e:
        print("exception:", type(e).__name__, e, flush=True)
    print("survived", flush=True)

else:
    print("unknown scenario", scenario, file=sys.stderr)
    sys.exit(99)

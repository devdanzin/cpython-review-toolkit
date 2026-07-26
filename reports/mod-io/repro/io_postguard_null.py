"""Post-guard NULL probes for Modules/_io.

Shape: a C method checks its state guard (CHECK_INITIALIZED / CHECK_CLOSED) at
entry, then runs arbitrary user Python (a flush(), a raw method, a `closed`
property), and afterwards keeps using `self->raw` -- which detach() has NULLed
in the meantime.

Usage:  io_postguard_null.py <scenario> <backend>
        backend in {io, _pyio}
Each scenario runs in its own process; the driver reports the exit code.
"""

import sys

scenario = sys.argv[1]
backend = sys.argv[2]

if backend == "_pyio":
    import _pyio as iomod
else:
    import io as iomod


# --- helper: a raw stream whose methods can re-enter -------------------------
class Raw(iomod.BytesIO):
    """A BytesIO subclass we can hang re-entrant hooks off."""

    hook = None
    fired = False

    def _fire(self):
        cls = type(self)
        if cls.hook is not None and not cls.fired:
            cls.fired = True
            cls.hook()

    def read(self, *a):
        self._fire()
        return super().read(*a)

    def readinto(self, b):
        self._fire()
        return super().readinto(b)

    def readall(self):
        self._fire()
        return super().readall()

    def truncate(self, pos=None):
        self._fire()
        return super().truncate(pos)


def detach_once(buf):
    """Return a flush() that detaches exactly once, then is inert.

    detach() itself calls _PyFile_Flush(self) -> self.flush(), so the flag is
    required or we recurse forever.
    """
    state = {"done": False}

    def flush(self):
        if not state["done"]:
            state["done"] = True
            self.detach()

    return flush


# --- S1: _io__Buffered_close_impl reads self->raw at bufferedio.c:591 --------
# close() drops the buffered lock, runs _PyFile_Flush (user flush()), retakes
# the lock, and then calls self->raw.close() with no re-check.
if scenario == "close_after_detach":
    done = [False]

    class B(iomod.BufferedReader):
        def flush(self):
            if not done[0]:
                done[0] = True
                self.detach()

    b = B(iomod.BytesIO(b"x" * 64))
    print("before close", flush=True)
    b.close()
    print("survived: close() returned", flush=True)


# --- S2: _io__Buffered_truncate_impl reads self->raw at bufferedio.c:1485 ----
# truncate() -> buffered_flush_and_rewind_unlocked() runs the raw's write();
# our raw detaches from inside truncate()'s own flush path.
elif scenario == "truncate_after_detach":
    done = [False]

    class B(iomod.BufferedWriter):
        def flush(self):
            if not done[0]:
                done[0] = True
                self.detach()
            else:
                super().flush()

    b = B(iomod.BytesIO())
    b.write(b"hello world")
    print("before truncate", flush=True)
    b.truncate(4)
    print("survived: truncate() returned", flush=True)


# --- S3: _bufferedreader_read_all reads self->raw at :1713 / :1748 ----------
# The read loop re-reads self->raw on every iteration; the raw's own read()
# detaches mid-loop.
elif scenario == "readall_after_detach":
    holder = {}

    class Raw2(iomod.RawIOBase):
        n = 0

        def readable(self):
            return True

        def read(self, size=-1):
            type(self).n += 1
            if type(self).n == 1:
                holder["b"].detach()
                return b"chunk"
            return b""

        def readall(self):
            return None  # force the read() loop rather than the readall path

    class B(iomod.BufferedReader):
        def flush(self):
            pass  # inert, so detach()'s internal _PyFile_Flush succeeds

    b = B(Raw2())
    holder["b"] = b
    print("before read", flush=True)
    b.read()
    print("survived: read() returned", flush=True)


# --- S4: _io__Buffered_seek_impl reads self->raw at bufferedio.c:1389 -------
# CHECK_CLOSED runs buffered_closed() -> PyObject_GetAttr(self->raw,'closed'),
# a *property* on the raw, i.e. arbitrary Python -- which detaches. Then :1389
# passes the now-NULL self->raw to _PyIOBase_check_seekable.
elif scenario == "seek_after_closed_property":
    holder = {}

    class Raw3(iomod.BytesIO):
        fired = False

        @property
        def closed(self):
            cls = type(self)
            if not cls.fired:
                cls.fired = True
                holder["b"].detach()
            return False

    class B(iomod.BufferedReader):
        def flush(self):
            pass

    b = B(Raw3(b"x" * 64))
    holder["b"] = b
    print("before seek", flush=True)
    b.seek(0, 2)
    print("survived: seek() returned", flush=True)


# --- S5: control -- does detach() itself still reproduce the seeded lead? ----
elif scenario == "detach_seeded":
    done = [False]

    class B(iomod.BufferedReader):
        def flush(self):
            if not done[0]:
                done[0] = True
                self.detach()

    b = B(iomod.BytesIO(b"x" * 64))
    print("before detach", flush=True)
    r = b.detach()
    print("survived: detach() returned", r, flush=True)

else:
    print("unknown scenario", scenario, file=sys.stderr)
    sys.exit(99)

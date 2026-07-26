"""_io._Buffered.close() drops its own lock at bufferedio.c:581, drives an
arbitrary user flush() at :582, then dereferences self->raw at :591 with no
re-check.

    :581  LEAVE_BUFFERED(self)                       /* lock dropped */
    :582  r = _PyFile_Flush((PyObject *)self);       /* user Python runs here */
    :583  if (!ENTER_BUFFERED(self)) return NULL;
    ...
    :591  res = PyObject_CallMethodNoArgs(self->raw, &_Py_ID(close));

A Python subclass that overrides flush() and calls detach() from it sets
self->raw = NULL (bufferedio.c:626) inside that window.

Guarded twin: _io_TextIOWrapper_close_impl (textio.c:3257) does the identical
"flush then close the underlying object" sequence but reaches the underlying
object via buffer_callmethod_noargs -> buffer_access_safe (textio.c:740), which
re-checks CHECK_ATTACHED after the re-entrancy point.

usage: python io_close_flush_detach.py [io|_pyio]
"""

import sys

backend = sys.argv[1] if len(sys.argv) > 1 else "io"
if backend == "io":
    import io as mod
else:
    import _pyio as mod

print(f"backend={backend}")


class Raw(mod.RawIOBase):
    def readable(self):
        return True

    def writable(self):
        return True

    def seekable(self):
        return False

    def readinto(self, b):
        return 0

    def write(self, b):
        return len(b)


class B(mod.BufferedWriter):
    armed = True

    def flush(self):
        if B.armed:
            B.armed = False
            print("  B.flush(): calling self.detach()")
            try:
                raw = self.detach()
                print("  B.flush(): detach() ->", type(raw).__name__)
            except BaseException as e:
                print(f"  B.flush(): detach() raised {type(e).__name__}: {e}")
        return super().flush()


b = B(Raw())
print("  calling b.close() ...")
try:
    res = b.close()
    print("  survived, close() ->", res)
except BaseException as e:
    print(f"  close() raised {type(e).__name__}: {e}")
print("  end of script")

"""_io._Buffered.close() re-reads self->raw at bufferedio.c:591 after two
re-entrancy points, with no re-check.

Path (all pure Python, no _testcapi):

  b._finalizing = True          # bufferedio.c:2578 is a WRITABLE Py_T_BOOL member
  b.close()
    -> bufferedio.c:573 self->finalizing is true
    -> :574 _io__Buffered__dealloc_warn_impl
       -> :491 PyObject_CallMethodOneArg(self->raw, "_dealloc_warn", self)
          -> USER PYTHON: raw._dealloc_warn(b) calls b.detach()
             -> :626 self->raw = NULL; :628 self->ok = 0
       -> :495 PyErr_Clear()          <-- eats any error the user code raised
    -> :578 PyErr_Clear()             <-- dead (the impl always returns Py_None)
    -> :582 _PyFile_Flush(self)       <-- second re-entrancy point
    -> :591 PyObject_CallMethodNoArgs(self->raw, "close")   <-- self->raw is NULL

Guarded twin, same operation, sibling file: _io_TextIOWrapper_close_impl
(textio.c:3257) reaches its buffer through buffer_callmethod_noargs ->
buffer_access_safe (textio.c:740), which re-checks CHECK_ATTACHED after the
re-entrancy point and raises ValueError instead of dereferencing NULL.

usage: python io_close_dealloc_warn_detach.py [buffered|textio]
"""

import sys

which = sys.argv[1] if len(sys.argv) > 1 else "buffered"
import io

print(f"variant={which}")


class Raw(io.RawIOBase):
    target = None

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

    def _dealloc_warn(self, source):
        t = Raw.target
        if t is not None:
            Raw.target = None
            print("  raw._dealloc_warn: calling detach() on the caller")
            try:
                got = t.detach()
                print("  raw._dealloc_warn: detach() ->", type(got).__name__)
            except BaseException as e:
                print(f"  raw._dealloc_warn: detach() raised {type(e).__name__}: {e}")
        return None


if which == "buffered":
    r = Raw()
    b = io.BufferedWriter(r)
    Raw.target = b
    b._finalizing = True
    print("  calling b.close() ...")
    res = b.close()
    print("  survived, close() ->", res)
else:
    # textio: the buffer's _dealloc_warn detaches the TextIOWrapper
    class Buf(io.BufferedWriter):
        target = None

        def _dealloc_warn(self, source):
            t = Buf.target
            if t is not None:
                Buf.target = None
                print("  buffer._dealloc_warn: calling detach() on the caller")
                try:
                    got = t.detach()
                    print("  buffer._dealloc_warn: detach() ->", type(got).__name__)
                except BaseException as e:
                    print(
                        f"  buffer._dealloc_warn: detach() raised "
                        f"{type(e).__name__}: {e}"
                    )
            return None

    buf = Buf(Raw())
    tw = io.TextIOWrapper(buf)
    Buf.target = tw
    tw._finalizing = True
    print("  calling tw.close() ...")
    try:
        res = tw.close()
        print("  survived, close() ->", res)
    except BaseException as e:
        print(f"  close() raised {type(e).__name__}: {e}")

print("  end of script")

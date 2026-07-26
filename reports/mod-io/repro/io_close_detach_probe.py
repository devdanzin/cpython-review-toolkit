"""Probe: _io._Buffered.close() calls a method ON self->raw after a user flush detached it.

gh-143008 / gh-142594 fixed exactly this shape in textio.c by introducing
buffer_access_safe() (Modules/_io/textio.c:740, applied at 6 sites). The fix was
never propagated to the Buffered* family in bufferedio.c.

_io__Buffered_close_impl (Modules/_io/bufferedio.c:~552):

    LEAVE_BUFFERED(self)                                  /* drops the lock */
    r = _PyFile_Flush((PyObject *)self);                  /* runs user Python */
    if (!ENTER_BUFFERED(self)) { return NULL; }           /* retakes the lock */
    ...
    res = PyObject_CallMethodNoArgs(self->raw, &_Py_ID(close));   /* NO re-check */

A user flush() that calls detach() sets self->raw = NULL, so the call above
dispatches a method on NULL -- a dereference, not merely a NULL return.

Usage:  python io_close_detach_probe.py [close|detach|truncate]
"""

import sys


def _mk(mode):
    import io

    class Evil(io.BufferedWriter):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.armed = True

        def flush(self):
            if self.armed:
                self.armed = False
                # detach() sets self->raw = NULL and self->ok = 0
                inner = super().detach()
                print("    inner detach -> %s" % type(inner).__name__, file=sys.stderr)
            return None

    return Evil(io.BytesIO())


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "close"
    if which == "truncate":
        # the ORIGINAL gh-143008 shape, on the textio side -- expected FIXED
        import io

        class Raw(io.BytesIO):
            def __init__(self):
                super().__init__()
                self._done = False

            def flush(self):
                if not self._done:
                    self._done = True
                    wrap.detach()
                return None

        wrap = io.TextIOWrapper(Raw())
        try:
            wrap.truncate(0)
        except BaseException as exc:
            print("truncate raised %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        else:
            print("truncate survived", file=sys.stderr)
        return 0

    e = _mk(which)
    print("calling %s()" % which, file=sys.stderr)
    try:
        out = getattr(e, which)()
        print("%s -> %r" % (which, out), file=sys.stderr)
    except BaseException as exc:
        print("%s raised %s: %s" % (which, type(exc).__name__, exc), file=sys.stderr)
    print("survived", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

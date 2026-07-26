"""Probe: _io._Buffered.detach re-entrancy after _PyFile_Flush.

textio's detach guards the post-flush read with buffer_access_safe() and an
explicit comment ("_PyFile_Flush could detach before returning"). bufferedio's
detach does `raw = self->raw` straight after the flush with no re-check.
"""

import io
import sys


class Evil(io.BufferedReader):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.armed = True

    def flush(self):
        if self.armed:
            self.armed = False
            # re-entrant detach from inside the flush the outer detach drives
            inner = super().detach()
            print("inner detach ->", type(inner).__name__, file=sys.stderr)
        return None


raw = io.BytesIO(b"abc")
e = Evil(raw)
print("calling outer detach", file=sys.stderr)
try:
    out = e.detach()
    print("outer detach ->", repr(out), file=sys.stderr)
except BaseException as exc:
    print("outer detach raised %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
print("survived", file=sys.stderr)

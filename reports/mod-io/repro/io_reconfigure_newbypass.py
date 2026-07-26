"""FIX candidate: _io.TextIOWrapper.reconfigure() has NO CHECK_ATTACHED.

Modules/_io/textio.c:1439 _io_TextIOWrapper_reconfigure_impl is the only
TextIOWrapper method with no CHECK_ATTACHED / CHECK_INITIALIZED at the top.
Its only accidental protection is _PyFile_Flush() at :1490, which normally
dispatches to _io_TextIOWrapper_flush_impl (which DOES CHECK_ATTACHED).
A pure-Python subclass that overrides flush() removes that protection.

Then textiowrapper_change_encoding (:1358) runs on a zeroed object:

    if (encoding == Py_None) {
        encoding = self->encoding;      /* NULL */
        if (errors == Py_None)
            errors = self->errors;      /* NULL */
        Py_INCREF(encoding);            /* textio.c:1369 -> Py_INCREF(NULL) */
    }
    ...
    Py_INCREF(errors);                  /* textio.c:1380 (scanner finding) */

Trigger the encoding==None branch by passing only `newline`.
"""

import io
import os
if os.environ.get("PYIO"): import _pyio as io
import sys

mode = sys.argv[1] if len(sys.argv) > 1 else "newline"


class S(io.TextIOWrapper):
    def __init__(self, *a, **kw):
        pass                       # deliberately skips super().__init__()

    def flush(self):
        return None                # removes the accidental CHECK_ATTACHED


t = S()
print("constructed, ok==0 (uninitialized)", file=sys.stderr)
sys.stderr.flush()

if mode == "newline":
    # encoding is None and errors is None, but newline_changed == 1, so
    # change_encoding does NOT take the "no change" early return.
    r = t.reconfigure(newline="\n")
else:
    r = t.reconfigure(encoding="utf-8")

print("survived: %r" % (r,), file=sys.stderr)

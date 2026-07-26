"""textio.c: self->snapshot NULLed after its own guard, inside tell().

_io_TextIOWrapper_tell_impl guards at textio.c:2857
    if (self->decoder == NULL || self->snapshot == NULL) return posobj;
then converts the *user-supplied* posobj at :2863
    cookie.start_pos = PyLong_AsLongLong(posobj);
PyLong_AsLongLong falls back to __index__ for a non-int, so arbitrary Python
runs between the guard and
    :2872  assert(PyTuple_Check(self->snapshot));      <- debug-only
    :2873  PyArg_ParseTuple(self->snapshot, "iO", ...); <- re-read, unguarded

_io_TextIOWrapper_write_impl:1870 does Py_CLEAR(self->snapshot), so an
__index__ that writes one character NULLs it inside that window.

Usage: io_textio_snapshot.py <backend>
"""

import sys

backend = sys.argv[1] if len(sys.argv) > 1 else "io"
if backend == "_pyio":
    import _pyio as iomod
else:
    import io as iomod

state = {}


class EvilIndex:
    fired = False

    def __index__(self):
        cls = type(self)
        if not cls.fired:
            cls.fired = True
            # textio.c:1870 -> Py_CLEAR(self->snapshot)
            state["f"].write("x")
        return 0


class Buf(iomod.BytesIO):
    """tell() hands back an object that only becomes an int via __index__."""

    def tell(self):
        return EvilIndex()


f = iomod.TextIOWrapper(Buf(b"hello\nworld\n"), encoding="utf-8")
state["f"] = f

f.read(1)  # establish self->decoder and self->snapshot

print("before tell", flush=True)
r = f.tell()
print("survived: tell() returned", r, flush=True)

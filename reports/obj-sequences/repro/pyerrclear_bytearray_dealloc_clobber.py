"""obj-sequences / pyerr-clear-auditor — what `bytearray_dealloc` does to a
pending exception when its `ob_exports > 0` branch fires.

    Objects/bytearrayobject.c:1209-1214
        if (self->ob_exports > 0) {
            PyErr_SetString(PyExc_SystemError,
                            "deallocated bytearray object has exported buffers");
            PyErr_Print();
        }

The branch is NOT reachable from pure Python: every in-tree `ob_exports++` is
bracketed inside one function on a live object (7 brackets, all verified
leak-free), and `bytearray_getbuffer_lock_held` reaches `ob_exports++` only
through `PyBuffer_FillInfo`, which takes a strong reference to `self` -- so a
leaked export also leaks the reference that keeps the object alive.

Reaching it therefore requires the *extension bug the branch exists to
diagnose*: `PyObject_GetBuffer()` followed by a `Py_DECREF` with no matching
`PyBuffer_Release()`.  This script produces exactly that with ctypes, so the
question "when the branch does fire, what happens to a live exception?" can be
answered by measurement instead of by argument.

Usage:  <python> pyerrclear_bytearray_dealloc_clobber.py [live|clean]
"""

import ctypes
import sys


class Py_buffer(ctypes.Structure):
    _fields_ = [
        ("buf", ctypes.c_void_p),
        ("obj", ctypes.c_void_p),
        ("len", ctypes.c_ssize_t),
        ("itemsize", ctypes.c_ssize_t),
        ("readonly", ctypes.c_int),
        ("ndim", ctypes.c_int),
        ("format", ctypes.c_char_p),
        ("shape", ctypes.POINTER(ctypes.c_ssize_t)),
        ("strides", ctypes.POINTER(ctypes.c_ssize_t)),
        ("suboffsets", ctypes.POINTER(ctypes.c_ssize_t)),
        ("internal", ctypes.c_void_p),
    ]


def _leak_export(b):
    """PyObject_GetBuffer without PyBuffer_Release, and cancel the strong
    reference PyBuffer_FillInfo took, so `b` can still reach tp_dealloc."""
    view = Py_buffer()
    rc = ctypes.pythonapi.PyObject_GetBuffer(
        ctypes.py_object(b), ctypes.byref(view), 0
    )
    assert rc == 0, rc
    ctypes.pythonapi.Py_DecRef(ctypes.py_object(b))
    return view


def live():
    """Drop the last reference to an over-exported bytearray while an exception
    is propagating."""

    def inner():
        b = bytearray(b"PAYLOAD-" * 4)
        _leak_export(b)
        raise ZeroDivisionError("REAL-EXCEPTION")

    try:
        inner()
    except BaseException as e:  # noqa: BLE001
        print(f"DEALLOC|live-exception|{type(e).__name__}|{e}")
    else:
        print("DEALLOC|live-exception|NO_EXCEPTION|")


def cpath():
    """Drop the last reference from a C error path that has ALREADY set an
    exception, so `bytearray_dealloc` runs with a genuinely pending exception.

    Objects/bytearrayobject.c:2216-2223 (bytearray_extend_impl):
        while ((item = PyIter_Next(it)) != NULL) {
            if (! _getbytevalue(item, &value)) {   /* sets TypeError */
                ...
                Py_DECREF(item);                   /* <- dealloc, TypeError pending */

    A Python-level ``__del__`` cannot be used to observe this: slot_tp_finalize
    (Objects/typeobject.c:11220) calls _PyErr_GetRaisedException() before running
    __del__ and restores afterwards, so a Python finalizer always sees a clear
    slot.  `bytearray_dealloc` is a real C tp_dealloc and has no such bracket.
    """

    class It:
        """A generator would keep `b` alive in its suspended frame; a plain
        iterator's frame is cleared on return, so `item` really is the last
        reference and Py_DECREF(item) really does deallocate."""

        def __init__(self):
            self.done = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.done:
                raise StopIteration
            self.done = True
            b = bytearray(b"PAYLOAD-" * 4)
            _leak_export(b)
            return b

    try:
        bytearray(b"").extend(It())
        print("DEALLOC|c-error-path|NO_EXCEPTION|")
    except BaseException as e:  # noqa: BLE001
        print(f"DEALLOC|c-error-path|{type(e).__name__}|{e}")


def clean():
    """No exception in flight -- the branch's intended diagnostic case."""

    def inner():
        b = bytearray(b"PAYLOAD-" * 4)
        _leak_export(b)

    try:
        inner()
        print("DEALLOC|no-exception|returned-normally|")
    except BaseException as e:  # noqa: BLE001
        print(f"DEALLOC|no-exception|{type(e).__name__}|{e}")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "live"
    print(f"BUILD {sys.executable}")
    if what == "live":
        live()
    elif what == "clean":
        clean()
    elif what == "cpath":
        cpath()
    else:
        clean()
        live()
        cpath()

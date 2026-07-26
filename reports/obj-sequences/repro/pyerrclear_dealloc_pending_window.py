"""obj-sequences / pyerr-clear-auditor — is an exception actually PENDING in
`tstate->current_exception` when a tp_dealloc runs?

`bytearrayobject.c:1210` overwrites the pending exception with `PyErr_SetString`
and then consumes it with `PyErr_Print`.  That is only harmful if a destructor
can genuinely run with a live exception in the tstate slot.  The bug-shape
catalog asserts it can ("teardown commonly runs while an exception is already
being handled"); this measures it directly with `PyErr_Occurred()` called from
inside `__del__`, over several candidate windows.

Usage:  <python> pyerrclear_dealloc_pending_window.py
"""

import ctypes
import sys

ctypes.pythonapi.PyErr_Occurred.restype = ctypes.c_void_p
ctypes.pythonapi.PyErr_Occurred.argtypes = []

SEEN = []


class Probe:
    """Reports whether an exception is pending in the tstate slot at dealloc."""

    def __init__(self, tag):
        self.tag = tag

    def __del__(self):
        p = ctypes.pythonapi.PyErr_Occurred()
        SEEN.append((self.tag, "PENDING" if p else "clear"))


def w1_frame_unwind():
    def inner():
        _p = Probe("w1 frame-unwind after raise")
        raise ZeroDivisionError("x")

    try:
        inner()
    except ZeroDivisionError:
        pass


def w2_sort_key_failure():
    lst = [Probe("w2 list.sort key raises"), 1]
    del lst[1]

    def boom(_):
        raise ZeroDivisionError("x")

    try:
        lst.sort(key=boom)
    except ZeroDivisionError:
        pass
    lst.clear()


def w3_argument_tuple():
    def f(_x):
        raise ZeroDivisionError("x")

    try:
        f(Probe("w3 temp argument, callee raises"))
    except ZeroDivisionError:
        pass


def w4_failed_getattr():
    class C:
        pass

    c = C()
    try:
        # the Probe is the only reference; the attribute lookup fails after it
        # has been built and dropped
        getattr(c, "missing_" + str(id(Probe("w4 temp built then AttributeError"))))
    except AttributeError:
        pass


def w5_in_except_block():
    try:
        raise ZeroDivisionError("x")
    except ZeroDivisionError:
        _p = Probe("w5 inside except block (handled, not pending)")
        del _p


def w6_iterator_raises_mid_call():
    def gen():
        yield Probe("w6 dropped while generator raises")
        raise ZeroDivisionError("x")

    try:
        list(gen())
    except ZeroDivisionError:
        pass


def w7_c_error_path_decref():
    """bytearray_extend_impl:2217-2223 -- _getbytevalue() sets TypeError and the
    very next statements Py_DECREF the item, the iterator and the temp
    bytearray.  A generator makes `item` the only reference."""

    def gen():
        yield Probe("w7 bytearray.extend C error path")

    try:
        bytearray(b"").extend(gen())
    except TypeError:
        pass


def w8_join_error_path():
    """stringlib_bytes_join's error path: PyErr_Format is issued and the
    sequence/refs are dropped afterwards."""

    def gen():
        yield Probe("w8 bytes.join C error path")

    try:
        b"".join(gen())
    except TypeError:
        pass


if __name__ == "__main__":
    print(f"BUILD {sys.executable}")
    for fn in (w1_frame_unwind, w2_sort_key_failure, w3_argument_tuple,
               w4_failed_getattr, w5_in_except_block, w6_iterator_raises_mid_call,
               w7_c_error_path_decref, w8_join_error_path):
        SEEN.clear()
        fn()
        for tag, state in SEEN:
            print(f"WINDOW|{tag:46s}|{state}")
        if not SEEN:
            print(f"WINDOW|{fn.__name__:46s}|no dealloc observed")

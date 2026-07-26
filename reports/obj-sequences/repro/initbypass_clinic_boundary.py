"""Task (d): a __new__-bypassed bytearray reaching an Argument-Clinic converter.

`Objects/clinic/bytearrayobject.c.h` runs 18 arbitrary-Python converters
(`_PyNumber_Index`, `_PyEval_SliceIndex`, `PyObject_GetBuffer`) BEFORE
`Py_BEGIN_CRITICAL_SECTION(self)` and before the `_impl` in the `.c` is entered.
This probes what happens when the receiver is a `bytearray.__new__(bytearray)`
whose `ob_bytes_object`/`ob_start` are still NULL, and the converter's user code
mutates that same receiver.

Usage:  python initbypass_clinic_boundary.py <probe> | --list
"""

import sys


def mk():
    return bytearray.__new__(bytearray)


def p(name, val):
    print("PROBE:%s=%s" % (name, val))
    sys.stdout.flush()


def _idx(fn, value=0):
    class Idx:
        def __index__(self):
            fn()
            return value
    return Idx()


def _buf(fn, payload=b"x"):
    class Buf:
        def __buffer__(self, flags):
            fn()
            return memoryview(payload)
    return Buf()


# --- converter initialises the receiver before the impl runs ---------------

def d_resize_index_inits():
    b = mk()
    return "resize=%r -> b=%r" % (b.resize(_idx(lambda: b.__init__(b"AAAA"), 2)), b)


def d_insert_index_inits():
    b = mk()
    b.insert(_idx(lambda: b.__init__(b"AAAA"), 0), 66)
    return repr(b)


def d_pop_index_inits():
    b = mk()
    return "pop=%r b=%r" % (b.pop(_idx(lambda: b.__init__(b"AB"), 0)), b)


def d_hex_index_inits():
    b = mk()
    return "hex=%r b=%r" % (b.hex("_", _idx(lambda: b.__init__(b"ABCD"), 1)), b)


def d_find_sliceindex_inits():
    b = mk()
    return "find=%r b=%r" % (b.find(b"C", _idx(lambda: b.__init__(b"ABCD"), 0)), b)


def d_replace_buffer_inits():
    b = mk()
    return "replace=%r b=%r" % (b.replace(_buf(lambda: b.__init__(b"xyz")), b"Q"), b)


def d_removeprefix_buffer_inits():
    b = mk()
    return "removeprefix=%r b=%r" % (
        b.removeprefix(_buf(lambda: b.__init__(b"xyz"))), b)


# --- converter grows the still-NULL receiver (crash inside the converter) ---

def d_resize_index_appends():
    b = mk()
    return b.resize(_idx(lambda: b.append(1), 2))


def d_insert_index_extends():
    b = mk()
    b.insert(_idx(lambda: b.extend(b"AB"), 0), 66)
    return repr(b)


# --- the pre-lock converter as the delivery vehicle for the F4 counter bug --

def d_export_then_index_inits_then_resize():
    """memoryview live; __index__ runs __init__ which resets ob_exports to 0.

    The clinic converter runs BEFORE Py_BEGIN_CRITICAL_SECTION(self), so the
    reset lands before `_canresize` is consulted by the impl.
    """
    b = mk()
    mv = memoryview(b)
    out = []
    try:
        out.append("resize=%r" % b.resize(_idx(lambda: b.__init__(), 8)))
    except BaseException as exc:  # noqa: BLE001
        out.append("resize RAISED %s: %s" % (type(exc).__name__, exc))
    out.append("len(b)=%d" % len(b))
    out.append("mv_nbytes=%d" % mv.nbytes)
    try:
        out.append("mv_bytes=%r" % bytes(mv))
    except BaseException as exc:  # noqa: BLE001
        out.append("mv_bytes RAISED %s" % type(exc).__name__)
    try:
        mv.release()
        out.append("release=ok")
    except BaseException as exc:  # noqa: BLE001
        out.append("release RAISED %s" % type(exc).__name__)
    # counter health check
    try:
        b.extend(b"Z")
        out.append("post_extend=ok len=%d" % len(b))
    except BaseException as exc:  # noqa: BLE001
        out.append("post_extend %s" % type(exc).__name__)
    mv2 = memoryview(b)
    try:
        b.extend(b"Y")
        out.append("COUNTER_BROKEN: grew with a live view")
    except BufferError:
        out.append("counter_ok")
    mv2.release()
    return " | ".join(out)


PROBES = {k[2:]: v for k, v in sorted(globals().items())
          if k.startswith("d_") and callable(v)}


def main():
    if len(sys.argv) < 2 or sys.argv[1] == "--list":
        for k in PROBES:
            print(k)
        return
    name = sys.argv[1]
    try:
        p(name, PROBES[name]())
    except BaseException as exc:  # noqa: BLE001
        p(name, "RAISED %s: %s" % (type(exc).__name__, str(exc)[:90]))


if __name__ == "__main__":
    main()

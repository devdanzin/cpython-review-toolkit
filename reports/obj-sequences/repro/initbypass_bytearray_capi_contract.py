"""`bytearray.__new__(bytearray)` vs `bytearray()` -- value differential + C-API contract.

Two things this measures that a crash count does not:

 1. VALUE differential.  For every operation that does NOT crash, does the
    bypassed object behave identically to a normally-constructed empty
    bytearray?  (Task (e): a broken object can return wrong values instead of
    faulting.)

 2. C-API contract.  `PyByteArray_AsString()` is documented to return the
    object's NUL-terminated buffer; `Include/bytearrayobject.h:32` gives it no
    failure mode.  After commit 732224e1139 (gh-139871) `PyByteArray_AS_STRING`
    is a bare `ob_start` read with no empty-string fallback, so the bypassed
    object returns NULL.  `_testlimitedcapi.bytearray_asstring` is the in-tree
    consumer that shows what a real extension does with that.

Usage:  python initbypass_bytearray_capi_contract.py [values|capi]
"""

import sys


def mk():
    return bytearray.__new__(bytearray)


NON_CRASHING = [
    ("len", len),
    ("bool", bool),
    ("repr", repr),
    ("alloc", lambda b: b.__alloc__()),
    ("sizeof", lambda b: b.__sizeof__()),
    ("getsizeof", lambda b: sys.getsizeof(b)),
    ("reduce_ex2", lambda b: b.__reduce_ex__(2)),
    ("hex", lambda b: b.hex()),
    ("decode", lambda b: b.decode()),
    ("copy", lambda b: b.copy()),
    ("bytes", bytes),
    ("list", list),
    ("mv_bytes", lambda b: bytes(memoryview(b))),
    ("mv_nbytes", lambda b: memoryview(b).nbytes),
    ("eq_empty", lambda b: b == bytearray()),
    ("add", lambda b: b + b"AB"),
    ("join", lambda b: b.join([b"a", b"b"])),
    ("center8", lambda b: b.center(8)),
    ("split", lambda b: b.split()),
    ("strip", lambda b: b.strip()),
    ("take_bytes", lambda b: b.take_bytes()),
    ("resize0", lambda b: b.resize(0)),
    ("clear", lambda b: b.clear()),
    ("imul0", lambda b: b.__imul__(0)),
    ("imul3", lambda b: b.__imul__(3)),
    ("extend_empty", lambda b: b.extend(b"")),
    ("iadd_empty", lambda b: b.__iadd__(b"")),
    ("fromhex", lambda b: b.fromhex("41")),
]


def values():
    diffs = 0
    for name, fn in NON_CRASHING:
        outs = []
        for maker in (mk, bytearray):
            o = maker()
            try:
                outs.append(repr(fn(o)))
            except BaseException as exc:  # noqa: BLE001
                outs.append("RAISED %s" % type(exc).__name__)
        same = outs[0] == outs[1]
        if not same:
            diffs += 1
        print("PROBE:value:%-14s %-5s bypassed=%-28s normal=%s"
              % (name, "SAME" if same else "DIFF", outs[0][:28], outs[1][:28]))
    print("PROBE:value_diffs=%d of %d" % (diffs, len(NON_CRASHING)))
    sys.stdout.flush()


def capi():
    import ctypes
    py = ctypes.pythonapi
    py.PyByteArray_AsString.restype = ctypes.c_void_p
    py.PyByteArray_AsString.argtypes = [ctypes.py_object]
    print("PROBE:AsString_normal=%r" % py.PyByteArray_AsString(bytearray()))
    print("PROBE:AsString_bypassed=%r" % py.PyByteArray_AsString(mk()))

    py.PyByteArray_Size.restype = ctypes.c_ssize_t
    py.PyByteArray_Size.argtypes = [ctypes.py_object]
    print("PROBE:Size_bypassed=%r" % py.PyByteArray_Size(mk()))

    try:
        import _testlimitedcapi
    except ImportError as exc:
        print("PROBE:testlimitedcapi=unavailable (%s)" % exc)
        sys.stdout.flush()
        return
    for label, obj in (("normal", bytearray()), ("bypassed", mk())):
        try:
            r = _testlimitedcapi.bytearray_asstring(obj, 0)
            print("PROBE:tlc_asstring_%s=%r" % (label, r))
        except BaseException as exc:  # noqa: BLE001
            print("PROBE:tlc_asstring_%s=RAISED %s: %s"
                  % (label, type(exc).__name__, exc))
    # And the buffer-protocol view of the same object, for contrast.
    try:
        import _testbuffer  # noqa: F401
    except ImportError:
        pass
    sys.stdout.flush()


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "values"
    {"values": values, "capi": capi}[which]()

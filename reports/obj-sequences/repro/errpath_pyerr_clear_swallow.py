"""Error-path agent, slice obj-sequences: the three scan_error_paths findings.

  E1  Objects/bytearrayobject.c:1171  bytearray_richcompare  PyErr_Clear after
      PyObject_GetBuffer(self)  -- self side (bytearray subclass with __buffer__)
  E2  Objects/bytearrayobject.c:1177  bytearray_richcompare  PyErr_Clear after
      PyObject_GetBuffer(other) -- other side (any object with __buffer__)
  E3  Objects/bytes_methods.c:608     _Py_bytes_contains     PyErr_Clear after
      PyNumber_AsSsize_t(arg)   -- doubled: bytes AND bytearray

Each probe raises KeyboardInterrupt (never a TypeError, so no narrowing test
could legitimately swallow it) from user code and reports whether it survived.

Usage:  <python> errpath_pyerr_clear_swallow.py [probe]
        probes: e1 e2 e3 e3_bytearray e3_wrongtype all
"""

import sys

SENTINEL = "SWALLOWED"


def _run(name, fn):
    try:
        r = fn()
    except KeyboardInterrupt:
        print(f"PROBE:{name}=PROPAGATED_KeyboardInterrupt", flush=True)
        return
    except BaseException as exc:  # noqa: BLE001 - probe
        print(f"PROBE:{name}=REPLACED_BY_{type(exc).__name__}", flush=True)
        return
    print(f"PROBE:{name}={SENTINEL} returned={r!r}", flush=True)


# ---------------------------------------------------------------- E2 (other)
def probe_e2():
    class RaisingExporter:
        def __buffer__(self, flags):
            raise KeyboardInterrupt("from __buffer__")

        def __release_buffer__(self, view):
            pass

    b = bytearray(b"abc")
    other = RaisingExporter()
    _run("e2_eq", lambda: b == other)
    _run("e2_lt", lambda: b < other)


# ----------------------------------------------------------------- E1 (self)
def probe_e1():
    class BA(bytearray):
        def __buffer__(self, flags):
            raise KeyboardInterrupt("from subclass __buffer__")

        def __release_buffer__(self, view):
            pass

    b = BA(b"abc")
    # other must also pass PyObject_CheckBuffer for the self-GetBuffer to run
    _run("e1_eq", lambda: b == b"abc")
    _run("e1_lt", lambda: b < b"abd")


# --------------------------------------------------- E3 (_Py_bytes_contains)
class EvilIndexPlusBuffer:
    """__index__ raises; __buffer__ succeeds, so `x in b` returns a bool."""

    def __index__(self):
        raise KeyboardInterrupt("from __index__")

    def __buffer__(self, flags):
        return memoryview(b"ell")

    def __release_buffer__(self, view):
        pass


class EvilIndexOnly:
    """__index__ raises; no buffer -> the KeyboardInterrupt becomes TypeError."""

    def __index__(self):
        raise KeyboardInterrupt("from __index__")


def probe_e3():
    _run("e3_bytes", lambda: EvilIndexPlusBuffer() in b"hello")


def probe_e3_bytearray():
    _run("e3_bytearray", lambda: EvilIndexPlusBuffer() in bytearray(b"hello"))


def probe_e3_wrongtype():
    _run("e3_bytes_wrongtype", lambda: EvilIndexOnly() in b"hello")
    _run("e3_bytearray_wrongtype", lambda: EvilIndexOnly() in bytearray(b"hello"))


# ------------------------------------------------------- guarded-twin control
def probe_twin():
    """parse_args_finds_byte (bytes_methods.c:410-421) is the guarded twin of
    _Py_bytes_contains:604-608.  Same file, same PyNumber_AsSsize_t conversion
    of the same user argument -- but it PROPAGATES instead of clearing.
    bytes/bytearray .find/.count/.index/.rfind/.rindex all route through it.

    NOTE: the object must NOT expose a buffer, or parse_args_finds_byte returns
    at :406-408 before ever calling __index__ (that is not a swallow, it is a
    path that never raises)."""
    _run("twin_find", lambda: b"hello".find(EvilIndexOnly()))
    _run("twin_count", lambda: b"hello".count(EvilIndexOnly()))
    _run("twin_index", lambda: b"hello".index(EvilIndexOnly()))
    _run("twin_ba_find", lambda: bytearray(b"hello").find(EvilIndexOnly()))
    _run("twin_ba_count", lambda: bytearray(b"hello").count(EvilIndexOnly()))


PROBES = {
    "e1": probe_e1,
    "e2": probe_e2,
    "e3": probe_e3,
    "e3_bytearray": probe_e3_bytearray,
    "e3_wrongtype": probe_e3_wrongtype,
    "twin": probe_twin,
}


def main(argv):
    which = argv[1] if len(argv) > 1 else "all"
    names = list(PROBES) if which == "all" else [which]
    for n in names:
        PROBES[n]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""obj-sequences / pyerr-clear-auditor — class table for the five sites that
overwrite a user ``__buffer__`` exception with a fixed TypeError.

Sites under test (CPython @ 4f3be1b5777):
  S1  Objects/bytesobject.c:1551      _PyBytes_Concat            b"a" + x
  S2  Objects/bytesobject.c:3291      PyBytes_Concat             C API (via _testlimitedcapi)
  S3  Objects/bytearrayobject.c:314   PyByteArray_Concat         bytearray(b"a") + x
  S4  Objects/bytearrayobject.c:357   bytearray_iconcat_lock_held  ba += x
  S5  Objects/bytearrayobject.c:664   bytearray_setslice         ba.extend(x)

Guarded twins, same slice:
  TA  Objects/bytearrayobject.c:1013  bytearray___init___impl    bytearray(x)   [propagate verbatim]
  TB  Objects/bytes_methods.c:697     _Py_bytes_tailmatch        b"abc".startswith(x)  [narrow, then reformat]
  TB2 Objects/bytes_methods.c:697     _Py_bytes_tailmatch        bytearray(b"abc").endswith(x)
  TC  Objects/bytearrayobject.c:1062  bytearray___init___impl    bytearray(NoBufIter)  [narrow on GetIter]

Every probe raises the requested exception class from a PEP 688 ``__buffer__``
and reports what class actually reaches Python.

Usage:  <python> pyerrclear_buffer_typeerror_overwrite.py [table|contract|buffererror|clears|reach|all]

  table        5 exception classes x 5 sites + 4 twins
  contract     is the fixed TypeError load-bearing? (genuine wrong-type operands)
  buffererror  no adversarial dunder: a strided memoryview raises BufferError
  clears       the three sites scan_pyerr_clear itself flagged, same 5 classes
  reach        Objects/bytesobject.c:3291 via io.TextIOWrapper
"""

import sys

EXC_CLASSES = [
    ZeroDivisionError,
    KeyboardInterrupt,
    MemoryError,
    SystemExit,
    RecursionError,
    TypeError,  # the class the reformat exists for -- the control
]


class Raiser:
    """Passes PyObject_CheckBuffer; its bf_getbuffer raises EXC."""

    def __init__(self, exc):
        self.exc = exc

    def __buffer__(self, flags):
        raise self.exc("BOOM-from-__buffer__")

    def __release_buffer__(self, view):
        pass


class NoBufIter:
    """No buffer protocol at all; __iter__ raises EXC (drives the GetIter twin)."""

    def __init__(self, exc):
        self.exc = exc

    def __iter__(self):
        raise self.exc("BOOM-from-__iter__")


def _probe(fn):
    try:
        fn()
    except BaseException as e:  # noqa: BLE001 - measuring which class arrives
        ctx = type(e.__context__).__name__ if e.__context__ is not None else "None"
        cause = type(e.__cause__).__name__ if e.__cause__ is not None else "None"
        return type(e).__name__, str(e)[:60], ctx, cause
    return "NO_EXCEPTION", "", "None", "None"


def _sites(exc):
    tlc = None
    try:
        import _testlimitedcapi as tlc  # noqa: PLC0415
    except ImportError:
        pass

    def s2():
        if tlc is None:
            raise RuntimeError("_testlimitedcapi unavailable")
        # new=1 -> fresh uniquely-referenced bytes -> in-place branch at :3285
        tlc.bytes_concat(b"abc", Raiser(exc), 1)

    def s5():
        bytearray(b"a").extend(Raiser(exc))

    def ta():
        bytearray(Raiser(exc))

    return [
        ("S1 _PyBytes_Concat        bytesobject.c:1551", lambda: b"a" + Raiser(exc)),
        ("S2 PyBytes_Concat         bytesobject.c:3291", s2),
        ("S3 PyByteArray_Concat     bytearrayobject.c:314", lambda: bytearray(b"a") + Raiser(exc)),
        ("S4 bytearray_iconcat      bytearrayobject.c:357", lambda: _iadd(exc)),
        ("S5 bytearray_setslice     bytearrayobject.c:664", s5),
        ("TA bytearray___init___    bytearrayobject.c:1013", ta),
        ("TB _Py_bytes_tailmatch    bytes_methods.c:697  (bytes)", lambda: b"abc".startswith(Raiser(exc))),
        ("TB2 _Py_bytes_tailmatch   bytes_methods.c:697  (bytearray)",
         lambda: bytearray(b"abc").endswith(Raiser(exc))),
        ("TC bytearray___init___    bytearrayobject.c:1062 (GetIter)",
         lambda: bytearray(NoBufIter(exc))),
    ]


def _iadd(exc):
    ba = bytearray(b"a")
    ba += Raiser(exc)


def table():
    print(f"BUILD {sys.executable}")
    for exc in EXC_CLASSES:
        print(f"\n--- raised from __buffer__ / __iter__: {exc.__name__} ---")
        for label, fn in _sites(exc):
            seen, msg, ctx, cause = _probe(fn)
            verdict = "SURVIVES" if seen == exc.__name__ else "DESTROYED"
            if exc is TypeError:
                verdict = "n/a"
            print(f"PROBE|{label:52s}|{exc.__name__:18s}|{seen:18s}|{verdict:9s}|"
                  f"ctx={ctx:18s}|{msg}")


def contract():
    """Is the fixed TypeError ever load-bearing? Non-buffer operands, and the
    CheckBuffer-passed-but-GetBuffer-failed case."""
    print(f"BUILD {sys.executable}")
    probes = [
        ("C1 b'a' + 5                        (S1, genuine wrong type)", lambda: b"a" + 5),
        ("C2 bytearray(b'a') + 5             (S3, genuine wrong type)", lambda: bytearray(b"a") + 5),
        ("C3 ba += 5                         (S4, genuine wrong type)", lambda: _iadd_int()),
        ("C4 ba.extend(5)                    (S5 -- does it reach :664?)",
         lambda: bytearray(b"a").extend(5)),
        ("C5 ba.extend(memoryview(b'abcd')[::2])  (S5, CheckBuffer ok, GetBuffer TypeError)",
         lambda: bytearray(b"a").extend(memoryview(b"abcd")[::2])),
        ("C6 b'a' + memoryview(b'abcd')[::2] (S1, CheckBuffer n/a, GetBuffer TypeError)",
         lambda: b"a" + memoryview(b"abcd")[::2]),
        ("C7 bytearray(memoryview(b'abcd')[::2]) (TA twin, same input)",
         lambda: bytearray(memoryview(b"abcd")[::2])),
        ("C8 b'abc'.startswith(5)            (TB twin, genuine wrong type)",
         lambda: b"abc".startswith(5)),
        ("C9 bytearray(5.5)                  (TC twin, genuine wrong type)",
         lambda: bytearray(5.5)),
    ]
    for label, fn in probes:
        seen, msg, ctx, cause = _probe(fn)
        print(f"CONTRACT|{label:58s}|{seen:16s}|{msg}")


def _iadd_int():
    ba = bytearray(b"a")
    ba += 5


def buffererror():
    """No adversarial dunder at all.

    A strided memoryview makes bf_getbuffer raise BufferError under PyBUF_SIMPLE.
    The five sites report TypeError; the in-slice twins report the real
    BufferError with the real diagnosis.
    """
    print(f"BUILD {sys.executable}")
    mv = memoryview(b"abcdef")[::2]
    probes = [
        ("S1 b'a' + mv                       bytesobject.c:1551", lambda: b"a" + mv),
        ("S3 bytearray(b'a') + mv            bytearrayobject.c:314",
         lambda: bytearray(b"a") + mv),
        ("S4 ba += mv                        bytearrayobject.c:357", lambda: _iadd_mv(mv)),
        ("S5 ba.extend(mv)                   bytearrayobject.c:664",
         lambda: bytearray(b"a").extend(mv)),
        ("TB b'abcdef'.startswith(mv)        bytes_methods.c:697", lambda: b"abcdef".startswith(mv)),
        ("TB2 bytearray(b'abcdef').endswith(mv)  bytes_methods.c:697",
         lambda: bytearray(b"abcdef").endswith(mv)),
        ("T- b'abcdef'.find(mv)              bytes_methods.c:469 (propagates)",
         lambda: b"abcdef".find(mv)),
        ("T- mv in b'abcdef'                 bytes_methods.c:609 (propagates)",
         lambda: mv in b"abcdef"),
        ("TA bytearray(mv)                   bytearrayobject.c:1013 (PyBUF_FULL_RO)",
         lambda: bytearray(mv)),
    ]
    for label, fn in probes:
        seen, msg, ctx, cause = _probe(fn)
        print(f"BUFERR|{label:58s}|{seen:16s}|{msg}")


def _iadd_mv(mv):
    ba = bytearray(b"a")
    ba += mv


def clears():
    """The three sites scan_pyerr_clear itself flagged, over the same 5 classes.

      bytearrayobject.c:1171  bytearray_richcompare  (self  is a subclass)
      bytearrayobject.c:1177  bytearray_richcompare  (other is any exporter)
      bytes_methods.c:608     _Py_bytes_contains     (PyNumber_AsSsize_t)

    plus the four narrowed clears in the same four files, as the twins.
    """
    print(f"BUILD {sys.executable}")

    class EvilIndexPlusBuffer:
        """Has BOTH __index__ (raises) and __buffer__ (works): after the clear
        at bytes_methods.c:608 the retry succeeds and `in` returns a bool."""

        def __init__(self, exc):
            self.exc = exc

        def __index__(self):
            raise self.exc("BOOM-from-__index__")

        def __buffer__(self, flags):
            return memoryview(b"ell")

        def __release_buffer__(self, view):
            pass

    class EvilIndexOnly:
        def __init__(self, exc):
            self.exc = exc

        def __index__(self):
            raise self.exc("BOOM-from-__index__")

    for exc in EXC_CLASSES:
        def sub_buffer():
            class BA(bytearray):
                def __buffer__(self, flags):
                    raise exc("BOOM-from-subclass-__buffer__")

                def __release_buffer__(self, view):
                    pass

            return BA(b"abc") == b"abc"

        probes = [
            ("K1 bytearray_richcompare :1171 (self subclass)", sub_buffer),
            ("K2 bytearray_richcompare :1177 (other exporter)",
             lambda: bytearray(b"abc") == Raiser(exc)),
            ("K3 _Py_bytes_contains    :608  bytes",
             lambda: EvilIndexPlusBuffer(exc) in b"hello"),
            ("K4 _Py_bytes_contains    :608  bytearray",
             lambda: EvilIndexPlusBuffer(exc) in bytearray(b"hello")),
            ("K5 _Py_bytes_contains    :608  no fallback buffer",
             lambda: EvilIndexOnly(exc) in b"hello"),
            ("T1 twin parse_args_finds_byte  bytes_methods.c:419 (find)",
             lambda: b"hello".find(EvilIndexOnly(exc))),
            ("T2 twin bytearray___init__     :996 narrowed clear",
             lambda: bytearray(EvilIndexOnly(exc))),
        ]
        for label, fn in probes:
            seen, msg, ctx, cause = _probe(fn)
            if exc is TypeError:
                verdict = "n/a"
            elif seen == exc.__name__:
                verdict = "SURVIVES"
            else:
                verdict = "DESTROYED"
            print(f"CLEARS|{label:48s}|{exc.__name__:18s}|{seen:18s}|{verdict:9s}|{msg}")


def reach():
    """Is Objects/bytesobject.c:3291 reachable from pure Python?

    Modules/_io/textio.c:2032 calls PyBytes_Concat(&next_input, input_chunk)
    where input_chunk is whatever the underlying buffer's read1()/read() returned
    and next_input is decoder.getstate()[0].  The in-place branch needs
    next_input to be a uniquely-referenced exact bytes; input_chunk must pass
    PyObject_GetBuffer once (textio.c:1999) and fail it the second time.
    """
    import codecs
    import io

    class TwoFaced:
        """__buffer__ succeeds once (for textio.c:1999) then raises."""

        def __init__(self, data, exc):
            self.data = data
            self.exc = exc
            self.n = 0

        def __buffer__(self, flags):
            self.n += 1
            if self.n > 1:
                raise self.exc("BOOM-second-getbuffer")
            return memoryview(self.data)

        def __release_buffer__(self, view):
            pass

    class Buf(io.BytesIO):
        """Seekable/tellable so TextIOWrapper enables self->telling, but read1()
        hands back a non-bytes buffer-exporting object."""

        def __init__(self, payload):
            super().__init__(b"x" * 64)
            self.payload = payload
            self.done = False

        def read1(self, n=-1):
            if self.done:
                return b""
            self.done = True
            return self.payload

        def read(self, n=-1):
            return self.read1(n)

    class Dec(codecs.IncrementalDecoder):
        """Never touches `data`, so the only bf_getbuffer calls on the payload
        are textio.c:1999 and, later, PyBytes_Concat at bytesobject.c:3290."""

        def decode(self, data, final=False):
            return "AB"

        def getstate(self):
            # fresh, non-interned, uniquely-referenced exact bytes ->
            # _PyObject_IsUniquelyReferenced(*pv) && PyBytes_CheckExact(*pv)
            return (bytes(bytearray(b"seedseed")), 0)

        def setstate(self, state):
            pass

        def reset(self):
            pass

    def _lookup(name):
        if name != "evil-getstate-codec":
            return None
        return codecs.CodecInfo(
            name=name,
            encode=lambda s, errors="strict": (s.encode("latin-1"), len(s)),
            decode=lambda b, errors="strict": (bytes(b).decode("latin-1"), len(b)),
            incrementaldecoder=Dec,
            incrementalencoder=codecs.getincrementalencoder("latin-1"),
            streamreader=codecs.getreader("latin-1"),
            streamwriter=codecs.getwriter("latin-1"),
        )

    codecs.register(_lookup)

    for exc in (KeyboardInterrupt, MemoryError, SystemExit, RecursionError,
                ZeroDivisionError):
        payload = TwoFaced(b"hello world", exc)
        t = io.TextIOWrapper(Buf(payload), encoding="evil-getstate-codec")
        t._CHUNK_SIZE = 8
        seen, msg, ctx, cause = _probe(lambda: t.read(1))
        print(f"REACH|textio.c:2032 -> PyBytes_Concat bytesobject.c:3291|"
              f"{exc.__name__:18s}|{seen:18s}|getbuffer_calls={payload.n}|{msg}")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "table"
    if what == "table":
        table()
    elif what == "contract":
        contract()
    elif what == "reach":
        reach()
    elif what == "buffererror":
        buffererror()
    elif what == "clears":
        clears()
    else:
        table()
        contract()
        buffererror()
        reach()

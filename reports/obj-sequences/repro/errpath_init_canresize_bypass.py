"""Error-path agent, slice obj-sequences.

bytearrayobject.c:930-938 -- `bytearray.__init__` skips the resize-to-0 (and
therefore the only `_canresize` check on the path) whenever `Py_SIZE(self)==0`,
then asserts `self->ob_exports == 0` at :938.

Question posed to this agent: is :938 a WRONG ASSERT or a MISSING CHECK, and is
the release-build BufferError the correct behaviour or an accident?

The probes below separate the two:

  a1  len-1 source string  -> the encoded bytes is the CACHED single-character
      PyBytes (bytesobject.c characters[]), so _PyObject_IsUniquelyReferenced
      fails at :967, control falls to bytearray_iconcat at :975, and *that*
      calls _canresize -> BufferError.  The BufferError is an ACCIDENT of the
      1-char bytes cache.
  a2  len-2 source string  -> the encoded bytes is fresh and uniquely
      referenced, the :967-973 fast path is taken, and __init__ mutates the
      bytearray WITH A LIVE MEMORYVIEW and raises nothing.

  a3  upstream's own new regression test body (test_reinit_with_view, added by
      PR #153498, merged 2026-07-24) run verbatim -- it uses "x", i.e. case a1.

Usage:  <python> errpath_init_canresize_bypass.py [a1|a2|a3|all]
"""

import sys


def _report(name, fn):
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001 - probe
        print(f"PROBE:{name}={type(exc).__name__}: {exc}", flush=True)
        return None
    print(f"PROBE:{name}=NO_EXCEPTION", flush=True)
    return True


def probe_a1():
    """len-1 encoded source: BufferError, but from bytearray_iconcat."""
    a = bytearray()
    mv = memoryview(a)
    _report("a1_init_1char", lambda: a.__init__("x", "ascii"))
    print(f"PROBE:a1_value={bytes(a)!r} mv_len={len(mv)}", flush=True)
    mv.release()


def probe_a2():
    """len-2 encoded source: the :967-973 fast path -- no _canresize anywhere."""
    a = bytearray()
    mv = memoryview(a)
    _report("a2_init_2char", lambda: a.__init__("xy", "ascii"))
    print(f"PROBE:a2_value={bytes(a)!r} mv_len={len(mv)}", flush=True)
    # The bytearray now holds a different backing buffer than the one the
    # memoryview was handed, yet ob_exports is still 1.
    _report("a2_append_while_exported", lambda: a.append(0x41))
    print(f"PROBE:a2_after_append={bytes(a)!r}", flush=True)
    mv.release()
    _report("a2_append_after_release", lambda: a.append(0x42))
    print(f"PROBE:a2_final={bytes(a)!r}", flush=True)


def probe_a3():
    """Upstream's new regression test, verbatim (Lib/test/test_bytes.py)."""
    a = bytearray()
    with memoryview(a):
        try:
            a.__init__("x", "ascii")
        except BufferError:
            print("PROBE:a3_upstream_test=PASSES (BufferError raised)", flush=True)
        else:
            print("PROBE:a3_upstream_test=FAILS (no BufferError)", flush=True)
    print(f"PROBE:a3_value={bytes(a)!r}", flush=True)


def probe_a4():
    """The mapper's original trigger, for the debug SIGABRT."""
    b = bytearray(b"AB")
    b.clear()
    mv = memoryview(b)
    print("PROBE:a4.setup=ok", flush=True)
    _report("a4_init_1char", lambda: b.__init__("x", "ascii"))
    mv.release()


def probe_a5():
    """Same, but 2 chars -> reaches :938 and then the fast path on release."""
    b = bytearray(b"AB")
    b.clear()
    mv = memoryview(b)
    print("PROBE:a5.setup=ok", flush=True)
    _report("a5_init_2char", lambda: b.__init__("xy", "ascii"))
    print(f"PROBE:a5_value={bytes(b)!r} mv_len={len(mv)}", flush=True)
    mv.release()


PROBES = {"a1": probe_a1, "a2": probe_a2, "a3": probe_a3, "a4": probe_a4, "a5": probe_a5}


def main(argv):
    which = argv[1] if len(argv) > 1 else "all"
    for n in (list(PROBES) if which == "all" else [which]):
        PROBES[n]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

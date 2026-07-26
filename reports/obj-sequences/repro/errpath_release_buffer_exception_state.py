"""Error-path agent, slice obj-sequences: the CPY-0180 error-path angle.

Task item (e): "PyBuffer_Release at bytearrayobject.c:2391 discards any exception
the user's __release_buffer__ raised.  Assess that as its own defect."

What actually happens is decided in Objects/typeobject.c, not in the slice:

  PyBuffer_Release            Objects/abstract.c   -> pb->bf_releasebuffer
  slot_bf_releasebuffer       Objects/typeobject.c:11489
  releasebuffer_call_python   Objects/typeobject.c:11420-11473
      PyObject *exc = PyErr_GetRaisedException();      <- saves the in-flight exc
      ret = vectorcall_method(&_Py_ID(__release_buffer__), stack, 2);
      if (ret == NULL) PyErr_FormatUnraisable(...);    <- REPORTS, not discards
      assert(!PyErr_Occurred());
      PyErr_SetRaisedException(exc);                   <- restores

So the callback's exception is *reported as unraisable* and the caller's own
exception state is preserved byte-for-byte.  Probes:

  p1  __release_buffer__ raises during bytearray.strip()   -> unraisable + result
  p2  same, but an exception is already pending when strip fails -> which wins
  p3  the "success after a failed callback" question (iii): does strip return a
      value even though the user callback raised?

Usage:  <python> errpath_release_buffer_exception_state.py [p1|p2|p3|all]
"""

import sys

seen = []


def _hook(unraisable):
    seen.append(type(unraisable.exc_value).__name__)
    print(f"PROBE:unraisable={type(unraisable.exc_value).__name__}", flush=True)


sys.unraisablehook = _hook


class RaisingReleaser:
    """Valid buffer, but __release_buffer__ raises."""

    def __buffer__(self, flags):
        return memoryview(b"\t")

    def __release_buffer__(self, view):
        raise KeyboardInterrupt("from __release_buffer__")


def _call(name, fn):
    try:
        r = fn()
    except BaseException as exc:  # noqa: BLE001 - probe
        print(f"PROBE:{name}=raised {type(exc).__name__}", flush=True)
        return None
    print(f"PROBE:{name}=returned {r!r}", flush=True)
    return r


def probe_p1():
    seen.clear()
    b = bytearray(b"\thello\t")
    _call("p1_bytearray_strip", lambda: b.strip(RaisingReleaser()))
    print(f"PROBE:p1_unraisable_seen={seen}", flush=True)


def probe_p2():
    """The caller's own exception must survive the callback's."""
    seen.clear()
    b = bytearray(b"\thello\t")
    mv = memoryview(b)   # pin: bytes(...) below is fine, but replace() will fail

    class Both:
        def __buffer__(self, flags):
            return memoryview(b"h")

        def __release_buffer__(self, view):
            raise KeyboardInterrupt("from __release_buffer__")

    # bytearray.replace acquires two buffers in the clinic wrapper; make the
    # SECOND one fail so a TypeError is pending when the FIRST is released.
    _call("p2_replace", lambda: b.replace(Both(), object()))
    print(f"PROBE:p2_unraisable_seen={seen}", flush=True)
    mv.release()


def probe_p3():
    """Question (iii): SUCCESS returned after the callback failed?"""
    seen.clear()
    b = bytearray(b"\thello\t")
    r = _call("p3_strip_result", lambda: b.strip(RaisingReleaser()))
    print(
        f"PROBE:p3_success_after_callback_failure="
        f"{'YES' if r is not None else 'NO'} unraisable={seen}",
        flush=True,
    )
    # Control: bytes (immutable) does the same thing at bytesobject.c:2127.
    r2 = _call("p3_bytes_strip_result", lambda: b"\thello\t".strip(RaisingReleaser()))
    print(f"PROBE:p3_bytes_success={'YES' if r2 is not None else 'NO'}", flush=True)


PROBES = {"p1": probe_p1, "p2": probe_p2, "p3": probe_p3}


def main(argv):
    which = argv[1] if len(argv) > 1 else "all"
    for n in (list(PROBES) if which == "all" else [which]):
        PROBES[n]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

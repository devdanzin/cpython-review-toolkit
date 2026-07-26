"""Error-path agent, slice obj-sequences.

bytearrayobject.c:924-928 -- `bytearray___init___impl` unconditionally resets
`self->ob_exports = 0` on the "first __init__" path (ob_bytes_object == NULL),
with no check that buffers are already exported.  That path is reachable from
Python via `bytearray.__new__(bytearray)`, which produces a zeroed object whose
buffer CAN be exported (Py_SIZE == 0 -> PyByteArray_AS_STRING returns the shared
empty string, so bytearray_getbuffer_lock_held succeeds and bumps ob_exports).

Sequence:
    b  = bytearray.__new__(bytearray)   # ob_bytes_object == NULL, ob_exports 0
    mv = memoryview(b)                  # ob_exports -> 1
    b.__init__()                        # :927 sets ob_exports = 0   <-- BUG
    mv.release()                        # :88  ob_exports-- -> -1

Consequences:
  * debug: assert(obj->ob_exports >= 0) at bytearrayobject.c:89 -> SIGABRT
  * release: ob_exports == -1 permanently, so _canresize (:115 `> 0`) never
    fires again -- the BufferError protection is disabled for the object's
    whole lifetime, even with a real live memoryview.

Usage:  <python> errpath_ob_exports_reset.py [d1|d2|all]
"""

import sys


def _call(name, fn):
    try:
        r = fn()
    except BaseException as exc:  # noqa: BLE001 - probe
        print(f"PROBE:{name}=raised {type(exc).__name__}: {exc}", flush=True)
        return None
    print(f"PROBE:{name}=ok -> {r!r}", flush=True)
    return r


def probe_d1():
    """Reach the negative counter."""
    b = bytearray.__new__(bytearray)
    mv = memoryview(b)
    print("PROBE:d1.setup=ok", flush=True)
    _call("d1_init", lambda: b.__init__())
    _call("d1_release", lambda: mv.release())
    print("PROBE:d1.survived_release=yes", flush=True)


def probe_d2():
    """After the counter goes negative, _canresize is permanently defeated:
    a live memoryview no longer blocks a resize, so the view is left pointing
    at a stale buffer."""
    b = bytearray.__new__(bytearray)
    mv0 = memoryview(b)
    b.__init__()          # ob_exports 1 -> 0
    mv0.release()         # ob_exports 0 -> -1
    print("PROBE:d2.setup=ok", flush=True)

    b.extend(b"AAAABBBB")
    live = memoryview(b)          # ob_exports -1 -> 0  (should be 1)
    print(f"PROBE:d2_live_view={bytes(live)!r}", flush=True)
    # With a correct counter this must raise BufferError.
    _call("d2_resize_with_live_view", lambda: b.extend(b"C" * (1 << 20)))
    print(f"PROBE:d2_view_after_resize={bytes(live)[:16]!r}", flush=True)
    print(f"PROBE:d2_len_after_resize={len(b)}", flush=True)
    _call("d2_release", lambda: live.release())


PROBES = {"d1": probe_d1, "d2": probe_d2}


def main(argv):
    which = argv[1] if len(argv) > 1 else "all"
    for n in (list(PROBES) if which == "all" else [which]):
        PROBES[n]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

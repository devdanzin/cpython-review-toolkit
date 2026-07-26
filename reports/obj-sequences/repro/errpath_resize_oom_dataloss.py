"""Error-path agent, slice obj-sequences.

bytearrayobject.c:280-291 -- when `_PyBytes_Resize` fails, the failure handler
installs the empty-bytes constant and sets `size = alloc = 0` BEFORE returning
-1.  MemoryError is raised, and the bytearray's entire contents are destroyed
as a side effect.

Contrast the sibling `bytearray_setslice_linear:588-605`, whose Issue #19578
comment goes to some length to describe exactly which state is restored on an
allocation failure -- the file clearly considers post-failure state a contract.

Also probes the follow-on: after that failure `bytearray_setslice_linear:597-599`
does `self->ob_start += growth` on the *already reset* ob_start, driving
ob_start below ob_bytes.

Usage:  <python> errpath_resize_oom_dataloss.py [r1|r2|all]
"""

import sys

HUGE = 1 << 46  # ~70 TB: large enough that the allocator refuses, small enough
                # to pass the PyByteArray_SIZE_MAX guard at bytearrayobject.c:265


def _call(name, fn):
    try:
        r = fn()
    except BaseException as exc:  # noqa: BLE001 - probe
        print(f"PROBE:{name}=raised {type(exc).__name__}", flush=True)
        return None
    print(f"PROBE:{name}=ok -> {r!r}", flush=True)
    return r


def probe_r1():
    """resize() failure destroys the contents."""
    b = bytearray(b"IMPORTANT-DATA")
    print(f"PROBE:r1_before={bytes(b)!r} len={len(b)}", flush=True)
    _call("r1_resize", lambda: b.resize(HUGE))
    print(f"PROBE:r1_after={bytes(b)!r} len={len(b)}", flush=True)


def probe_r2():
    """Same through append/extend growth (bytearray_setslice_linear:614)."""
    b = bytearray(b"IMPORTANT-DATA")
    print(f"PROBE:r2_before={bytes(b)!r} len={len(b)}", flush=True)
    # extend() cannot be used here (it would need a HUGE source object), so
    # drive the same bytearray_resize_lock_held growth through repeat instead.
    _call("r2_repeat", lambda: _grow(b))
    print(f"PROBE:r2_after={bytes(b)!r} len={len(b)}", flush=True)


def _grow(b):
    # b[len(b):len(b)] = <huge> needs a huge source; use repeat instead, which
    # goes through bytearray_repeat -> PyByteArray_Resize.
    b *= (HUGE // max(len(b), 1))
    return b


PROBES = {"r1": probe_r1, "r2": probe_r2}


def main(argv):
    which = argv[1] if len(argv) > 1 else "all"
    for n in (list(PROBES) if which == "all" else [which]):
        PROBES[n]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

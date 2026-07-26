"""Error-path agent, slice obj-sequences: the clinic wrapper boundary.

Objects/clinic/{listobject,bytearrayobject}.c.h run arbitrary user Python
(_PyNumber_Index, _PyEval_SliceIndex, PyObject_GetBuffer) BEFORE
Py_BEGIN_CRITICAL_SECTION and before the .c file is entered, and run
PyBuffer_Release (-> __release_buffer__) at the `exit:` label AFTER
Py_END_CRITICAL_SECTION, possibly with an exception already pending.

This probe checks, across that boundary:
  C1  converter raises      -> is the right exception delivered, and is the
                               per-object lock left clean (object still usable,
                               including from a second thread)?
  C2  2nd Py_buffer converter fails after the 1st succeeded -> is the 1st
                               released, and does the 2nd's exception survive
                               the 1st's __release_buffer__?
  C3  __release_buffer__ raises while the impl's exception is pending ->
                               which exception reaches Python?
  C4  __release_buffer__ raises on a SUCCESSFUL call -> is the result kept and
                               the callback's exception reported (not raised)?

Usage:  <python> errpath_clinic_boundary.py [c1|c2|c3|c4|all]
"""

import sys
import threading


class RaisingIndex:
    def __index__(self):
        raise KeyboardInterrupt("from __index__")


def _call(name, fn):
    try:
        r = fn()
    except BaseException as exc:  # noqa: BLE001 - probe
        print(f"PROBE:{name}=raised {type(exc).__name__}: {exc}", flush=True)
        return
    print(f"PROBE:{name}=returned {r!r}", flush=True)


# --------------------------------------------------------------------- C1
def probe_c1():
    """Converter runs before the critical section.  If it raises, the lock must
    never have been taken."""
    lst = [1, 2, 3]
    ba = bytearray(b"hello")

    _call("c1_list_insert", lambda: lst.insert(RaisingIndex(), 9))
    _call("c1_list_pop", lambda: lst.pop(RaisingIndex()))
    _call("c1_ba_pop", lambda: ba.pop(RaisingIndex()))
    _call("c1_ba_insert", lambda: ba.insert(RaisingIndex(), 1))
    _call("c1_ba_find_start", lambda: ba.find(b"e", RaisingIndex()))
    _call("c1_ba_find_end", lambda: ba.find(b"e", 0, RaisingIndex()))
    _call("c1_ba_resize", lambda: ba.resize(RaisingIndex()))
    _call("c1_ba_hex", lambda: ba.hex(":", RaisingIndex()))

    # Same thread: still usable?
    _call("c1_after_same_thread", lambda: (lst.copy(), bytes(ba)))

    # Other thread: a leaked per-object critical section would park here.
    done = []

    def worker():
        lst.append(4)
        ba.append(0x41)
        done.append(True)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5.0)
    print(
        f"PROBE:c1_other_thread={'ok' if done else 'HUNG'} "
        f"lst={lst!r} ba={bytes(ba)!r}",
        flush=True,
    )


# --------------------------------------------------------------------- C2
def probe_c2():
    """bytearray.replace(old, new): two Py_buffer converters.  Make the 2nd
    fail and check the 1st is released and its __release_buffer__ cannot eat
    the 2nd's exception."""
    released = []

    class Exporter:
        def __buffer__(self, flags):
            return memoryview(b"AA")

        def __release_buffer__(self, view):
            released.append("plain")

    class NoisyReleaser:
        def __buffer__(self, flags):
            return memoryview(b"AA")

        def __release_buffer__(self, view):
            released.append("noisy")
            raise ZeroDivisionError("from __release_buffer__")

    ba = bytearray(b"AABB")
    _call("c2_second_converter_fails", lambda: ba.replace(Exporter(), object()))
    print(f"PROBE:c2_released={released}", flush=True)

    released.clear()
    _call("c2_noisy_first_release", lambda: ba.replace(NoisyReleaser(), object()))
    print(f"PROBE:c2_noisy_released={released}", flush=True)


# --------------------------------------------------------------------- C3
def probe_c3():
    """Impl raises, then the clinic exit label runs __release_buffer__ which
    also raises.  Which exception reaches Python?"""

    class NoisyReleaser:
        def __buffer__(self, flags):
            return memoryview(b"A")

        def __release_buffer__(self, view):
            raise ZeroDivisionError("from __release_buffer__")

    ba = bytearray(b"AABB")
    mv = memoryview(ba)  # pin: the impl will fail with BufferError
    _call("c3_impl_fails_and_release_raises",
          lambda: ba.replace(NoisyReleaser(), b"CCCC"))
    mv.release()


# --------------------------------------------------------------------- C4
def probe_c4():
    """Successful call, but __release_buffer__ raises at the exit label."""

    class NoisyReleaser:
        def __buffer__(self, flags):
            return memoryview(b"A")

        def __release_buffer__(self, view):
            raise ZeroDivisionError("from __release_buffer__")

    ba = bytearray(b"AABB")
    _call("c4_success_release_raises", lambda: ba.replace(NoisyReleaser(), b"Z"))
    _call("c4_removeprefix", lambda: bytearray(b"AB").removeprefix(NoisyReleaser()))


PROBES = {"c1": probe_c1, "c2": probe_c2, "c3": probe_c3, "c4": probe_c4}


def main(argv):
    which = argv[1] if len(argv) > 1 else "all"
    for n in (list(PROBES) if which == "all" else [which]):
        PROBES[n]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

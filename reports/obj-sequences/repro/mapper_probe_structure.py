"""Phase-1 structural probes for slice obj-sequences.

Not bug reproducers -- these are measurements the include-graph-mapper cites.
Each probe prints PROBE:<name>=<result> and never raises out of main().
Run under an explicitly named interpreter, e.g.
  ~/projects/python_build_matrix/builds/release-gil-nojit/python this_file.py
"""

import sys


def probe_new_bypass_fields():
    """bytearray.__new__(bytearray): which methods survive an __init__ bypass?"""
    ba = bytearray.__new__(bytearray)
    for name, fn in [
        ("len", lambda: len(ba)),
        ("bool", lambda: bool(ba)),
        ("repr", lambda: repr(ba)),
        ("append", lambda: ba.append(1)),
    ]:
        try:
            fn()
            print(f"PROBE:new_bypass.{name}=ok", flush=True)
        except BaseException as exc:  # noqa: BLE001 - probe
            print(f"PROBE:new_bypass.{name}={type(exc).__name__}", flush=True)


def probe_resize_while_exported():
    """Does a live memoryview actually block every bytearray resize path?"""
    cases = [
        ("append", lambda b: b.append(1)),
        ("clear", lambda b: b.clear()),
        ("iadd", lambda b: b.__iadd__(b"x")),
        ("pop", lambda b: b.pop()),
        ("remove", lambda b: b.remove(65)),
        ("delslice", lambda b: b.__delitem__(slice(0, 1))),
        ("delextslice", lambda b: b.__delitem__(slice(0, 2, 2))),
        ("resize", lambda b: b.resize(9)),
        ("init_nonempty", lambda b: b.__init__(b"zz")),
    ]
    for name, fn in cases:
        b = bytearray(b"ABCDE")
        mv = memoryview(b)
        try:
            fn(b)
            print(f"PROBE:exported_resize.{name}=ALLOWED len={len(b)}", flush=True)
        except BaseException as exc:  # noqa: BLE001 - probe
            print(f"PROBE:exported_resize.{name}={type(exc).__name__}", flush=True)
        finally:
            mv.release()


def probe_init_fastpath_while_exported():
    """The bytearray___init___impl:1094 fast-append path, on an EMPTY exported bytearray."""
    b = bytearray(b"ABCDE")
    b.clear()  # size 0, but ob_alloc still > 0
    mv = memoryview(b)
    try:
        b.__init__([65, 66, 67])
        print(f"PROBE:init_fastpath_exported=ALLOWED len={len(b)} mvlen={len(mv)}")
    except BaseException as exc:  # noqa: BLE001 - probe
        print(f"PROBE:init_fastpath_exported={type(exc).__name__}")
    finally:
        mv.release()


def probe_release_buffer_runs_python():
    """Confirm PEP 688 __release_buffer__ really executes user code from PyBuffer_Release."""
    seen = []

    class Exporter:
        def __buffer__(self, flags):
            return memoryview(b"AB")

        def __release_buffer__(self, view):
            seen.append("released")

    b = bytearray(b"AABBAA")
    try:
        b.strip(Exporter())
    except BaseException as exc:  # noqa: BLE001 - probe
        print(f"PROBE:release_buffer.strip_raised={type(exc).__name__}")
    print(f"PROBE:release_buffer.callback_ran={bool(seen)}")


PROBES = {
    "new_bypass": probe_new_bypass_fields,
    "exported_resize": probe_resize_while_exported,
    "init_fastpath": probe_init_fastpath_while_exported,
    "release_buffer": probe_release_buffer_runs_python,
}


def main():
    # One probe per process: probe_new_bypass_fields is expected to SIGSEGV,
    # and a crash must not swallow the other probes' output.
    which = sys.argv[1] if len(sys.argv) > 1 else None
    if which is None:
        print(f"PROBE:interpreter={sys.version.splitlines()[0]}", flush=True)
        print("usage: <python> mapper_probe_structure.py <" + "|".join(PROBES) + ">", flush=True)
        return
    print(f"PROBE:interpreter={sys.version.splitlines()[0]}", flush=True)
    PROBES[which]()
    sys.stdout.flush()


if __name__ == "__main__":
    main()

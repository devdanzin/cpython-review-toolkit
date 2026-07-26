#!/usr/bin/env python3
"""Which byte of `struct _stoptheworld_state` does CPY-0186 have to hit?

Companion to `stw_runtime_oob_target.py`.  That script writes 24 bytes of 0xFF
over the whole `_PyRuntime._main_interpreter.stoptheworld` struct and hangs the
free-threaded builds.  This one writes **one byte** at a time so the persistent
fields can be told apart from the self-healing ones.

`stop_the_world()` (`Python/pystate.c:2439-2470`) re-initialises `requested`,
`thread_countdown`, `stop_event` and `requester` at the top of *every* pause:

    2452  stw->requested = 1;
    2453  stw->thread_countdown = 0;
    2454  stw->stop_event = (PyEvent){0};
    2455  stw->requester = _PyThreadState_GET();

so corrupting those between pauses is expected to be harmless.  The two fields
that are *not* re-initialised are:

    +0  PyMutex mutex     taken at :2450, released at :2520 -- a stale "locked"
                          byte deadlocks every future pause
    +3  bool is_global    set once at interpreter creation; decides both which
                          thread list is walked (`interp_for_stop_the_world`,
                          :2398) and whether the runtime rwmutex is taken as a
                          writer or a reader (:2445-2450)

Usage
    python stw_field_granularity.py <python>
"""

import subprocess
import sys

STW_OFFSET = {
    "release-ft-nojit": 106016,
    "debug-ft-nojit": 106016,
    "release-gil-nojit": 83800,
    "debug-gil-nojit": 83808,
}

FIELDS = [
    (0, "mutex"),
    (1, "requested"),
    (2, "world_stopped"),
    (3, "is_global"),
    (4, "stop_event"),
    (8, "thread_countdown"),
    (16, "requester"),
]

SIZE = 400_000
TIMEOUT = 20

CHILD = r'''
import sys, _testcapi, gc
SIZE, LO, N = {size}, {lo}, {n}
src = b'\xFF'
b = bytearray(b'x' * SIZE)
try:
    b[LO:SIZE] = src
except MemoryError:
    pass
b = bytearray(b'x' * SIZE)
_testcapi.set_nomemory(N, N + 1)
try:
    b[LO:SIZE] = src
    r = "no-exception"
except MemoryError:
    r = "MemoryError"
finally:
    try:
        _testcapi.remove_mem_hooks()
    except Exception:
        pass
try:
    _n = len(b)
except BaseException as e:
    _n = "EXC %s" % type(e).__name__
print("OP:%s len=%s" % (r, _n), flush=True)
for label, fn in (("freeze", gc.freeze), ("unfreeze", gc.unfreeze),
                  ("collect", gc.collect)):
    try:
        print("STW:%s=%r" % (label, fn()), flush=True)
    except BaseException as e:
        print("STW:%s=EXC %s" % (label, type(e).__name__), flush=True)
print("DONE", flush=True)
'''


def run(python, lo, n):
    src = CHILD.format(size=SIZE, lo=lo, n=n)
    try:
        return subprocess.run([python, "-c", src], capture_output=True,
                              text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired as e:
        class T:
            returncode = "TIMEOUT"
            stdout = (e.stdout or "") if isinstance(e.stdout, str) else (e.stdout or b"").decode(errors="replace")
            stderr = ""
        return T()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    python = sys.argv[1]
    build = next((b for b in STW_OFFSET if b in python), None)
    if build is None:
        print("cannot infer build from %r" % python)
        return 2
    base = STW_OFFSET[build]
    print(f"### build={build}  stoptheworld base offset = {base}")
    for off, name in FIELDS:
        lo = base + off
        best = None
        for n in range(0, 6):
            p = run(python, lo, n)
            if "EXC SystemError" in p.stdout or p.returncode == "TIMEOUT" or p.returncode not in (0, 1):
                best = (n, p)
                break
        if best is None:
            print(f"  +{off:<3d} {name:<18s} lo={lo}  -- no OOB-write index found in n=0..5")
            continue
        n, p = best
        out = " | ".join(x for x in p.stdout.splitlines() if x)
        print(f"  +{off:<3d} {name:<18s} lo={lo}  n={n}  rc={p.returncode!s:>8}  {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

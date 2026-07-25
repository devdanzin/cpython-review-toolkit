#!/usr/bin/env python3
"""P2-F18 driven through the REAL tier-2 optimizer -- no _testcapi.

This demonstrates the CONSEQUENCE rather than only the race: both writers of
`PyTypeObject.tp_watched` are the tier-2 optimizer itself, using the SAME
watcher id (`TYPE_WATCHER_ID == 0`, Python/optimizer_analysis.c:138):

  LOCKED writer   Python/optimizer_analysis.c:177   watch_type()
                  Python/optimizer_bytecodes.c:1462, :2215, :2236
                    -> PyType_Watch(0, T)   -> typeobject.c:1112  |= under TYPE_LOCK

  BARE writer     Python/optimizer_analysis.c:156   type_watcher_callback()
                    -> PyType_Unwatch(0, T) -> typeobject.c:1129  &=~ unlocked

`type_watcher_callback` is invoked from the watcher loop inside
`_PyType_Modified_Unlocked` (typeobject.c:1212-1222) -- i.e. it mutates the
very byte that loop is iterating -- and it runs on whichever thread modified
the type, while `watch_type` runs on whichever thread is optimizing a trace.

A LOST SET means `_Py_Executors_InvalidateDependency` is never reached for a
type a live executor guards on.

Needs a free-threaded build with tier 2 enabled
(`--disable-gil --enable-experimental-jit=interpreter`).

    PYTHON_GIL=0 ./python tsan_stress_tier2_watched.py 2> tsan_report.txt
"""
import sys
import threading
import time

THREADS = 8
HOT = 20_000
ROUNDS = 60


def _is_tsan_build():
    try:
        import sysconfig
        return "fsanitize=thread" in (sysconfig.get_config_var("CFLAGS") or "").lower()
    except Exception:
        return False


IS_TSAN = _is_tsan_build()
if IS_TSAN:
    THREADS = min(THREADS, 4)
    HOT = 6_000
    ROUNDS = 12

import warnings
warnings.filterwarnings("ignore", ".*GIL.*")


import _testinternalcapi


class Shared:
    """The type every optimized trace guards on and every mutator mutates."""
    def m(self):
        return 1

    k = 1


SHARED_INST = Shared()
stop = threading.Event()

# Distinct code objects so tier 2 forms MANY independent traces, all of which
# call watch_type() on the same shared type.
_SRC = """
def hot{n}(inst):
    total = 0
    for _ in range({hot}):
        total += inst.m()
        total += inst.k
        total += inst.k
    return total
"""
HOTFNS = []
for _n in range(12):
    _ns = {}
    exec(_SRC.format(n=_n, hot=HOT), _ns)
    HOTFNS.append(_ns["hot%d" % _n])


def optimizer_thread(idx):
    """Warm a trace (-> PyType_Watch, the LOCKED writer at typeobject.c:1112),
    then force re-optimization so the locked write keeps recurring."""
    fn = HOTFNS[idx % len(HOTFNS)]
    while not stop.is_set():
        fn(SHARED_INST)
        # drop the executors so the next call re-optimizes and re-watches
        _testinternalcapi.invalidate_executors(Shared)


def mutator_thread():
    """Each store to `Shared` runs _PyType_Modified_Unlocked, whose watcher
    loop (:1212) calls type_watcher_callback -> PyType_Unwatch (the BARE
    writer at :1129)."""
    i = 0
    while not stop.is_set():
        Shared.k = i
        # re-arm the version tag so the NEXT store reaches the watcher loop
        getattr(Shared, "k", None)
        i += 1


def warmup():
    """Quiet phase: let tier 2 actually build traces before mutation starts."""
    for fn in HOTFNS:
        fn(SHARED_INST)


if __name__ == "__main__":
    jit = getattr(sys, "_jit", None)
    print("tier-2 tp_watched stress (P2-F18 consequence)")
    print(f"  Python: {sys.version.splitlines()[0]}")
    print(f"  TSan build: {IS_TSAN}")
    if jit is None or not jit.is_enabled():
        print("  ERROR: tier 2 is NOT enabled on this build -- "
              "configure with --enable-experimental-jit=interpreter")
        sys.exit(2)
    print(f"  tier2: available={jit.is_available()} enabled={jit.is_enabled()}")
    print(f"  Threads: {THREADS}, hot={HOT}")
    print("  warming up traces (quiet, no mutation)...", flush=True)
    warmup()
    print("  warm.", flush=True)
    print()

    nopt = max(2, THREADS // 2)
    threads = [threading.Thread(target=optimizer_thread, args=(i,), daemon=True)
               for i in range(nopt)]
    threads += [threading.Thread(target=mutator_thread, daemon=True)
                for _ in range(max(2, THREADS // 2))]

    for t in threads:
        t.start()
    time.sleep(60 if IS_TSAN else 20)
    stop.set()
    for t in threads:
        t.join(timeout=30)
    print("Done. Check stderr for TSan warnings mentioning tp_watched / "
          "PyType_Watch / PyType_Unwatch / optimizer_analysis.c")

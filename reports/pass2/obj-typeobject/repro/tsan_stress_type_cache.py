#!/usr/bin/env python3
"""TSan stress test for CPython's method cache (`struct type_cache`).

Target: the ft-race-scanner's `type_cache_clear` finding.

    Objects/typeobject.c:6216-6230  update_cache()      -- the CONTRACT
        _Py_atomic_store_ptr_relaxed(&entry->value, value);
        _Py_atomic_store_ptr_relaxed(&entry->name,  Py_NewRef(name));
        /* "We must write the version last to avoid _Py_TryXGetStackRef()
            operating on an invalid (already deallocated) value ..." */
        _Py_atomic_store_uint32_release(&entry->version, version_tag);

    Objects/typeobject.c:988-990    type_cache_clear()  -- INVERTS IT
        entry->version = 0;                              <- version FIRST
        Py_XSETREF(entry->name, _Py_XNewRef(value));
        entry->value = NULL;                             <- value LAST
      ... and all three are PLAIN non-atomic stores.

    Objects/typeobject.c:6275-6281  _PyTypes_AfterFork() -- same plain stores.

The reader `_PyType_LookupStackRefAndVersion` (:6306-) uses
`_Py_atomic_load_uint32_acquire(&entry->version)` and
`_Py_atomic_load_ptr_relaxed(&entry->name)`, so every plain store above is a
plain-store-vs-atomic-load pair.

Python-level trigger for the clear path: `sys._clear_type_cache()`
(-> PyType_ClearCache -> _PyType_ClearCache -> type_cache_clear, :1021).

Run with a free-threaded TSan build of CPython:
    PYTHON_GIL=0 ./python tsan_stress_type_cache.py 2> tsan_report.txt
"""
import os
import signal
import sys
import threading
import time

THREADS = 8
ITERATIONS = 20_000
SCENARIO_TIMEOUT = 120


def _is_tsan_build():
    try:
        import sysconfig
        return "fsanitize=thread" in (sysconfig.get_config_var("CFLAGS") or "").lower()
    except Exception:
        return False


IS_TSAN = _is_tsan_build()
if IS_TSAN:
    THREADS = min(THREADS, 4)
    ITERATIONS = min(ITERATIONS, 300)

import warnings
warnings.filterwarnings("ignore", ".*GIL.*")
# sys._clear_type_cache() is deprecated; its DeprecationWarning drags in a
# first-time lazy import (linecache -> tokenize -> io -> abc.ABCMeta.__new__),
# i.e. CLASS CREATION on a worker thread. Under continuous stop-the-world type
# mutation that import starves and the scenario appears to hang. Suppress the
# warning AND pre-import, so the harness measures type mutation, not import.
warnings.simplefilter("ignore", DeprecationWarning)
import linecache  # noqa: E402
import tokenize   # noqa: E402,F401
import io         # noqa: E402,F401


def run_scenario(name, target_fns, thread_counts=None):
    print(f"  Running: {name}...", end=" ", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    if os.environ.get("STRESS_NO_FORK"):
        # TSan + os.fork() deadlocks inside the sanitizer runtime; run inline
        # and rely on one-scenario-per-process isolation from the driver.
        try:
            _run_scenario_threads(target_fns, thread_counts)
            print("OK", flush=True)
        except SystemExit:
            print("FAIL", flush=True)
        except BaseException as e:
            print("ERROR (%r)" % (e,), flush=True)
        return
    pid = os.fork()
    if pid == 0:
        try:
            _run_scenario_threads(target_fns, thread_counts)
            os._exit(0)
        except SystemExit as e:
            os._exit(e.code if isinstance(e.code, int) else 1)
        except BaseException:
            import traceback
            traceback.print_exc()
            os._exit(1)

    deadline = time.monotonic() + SCENARIO_TIMEOUT
    wait_status = None
    while time.monotonic() < deadline:
        r, status = os.waitpid(pid, os.WNOHANG)
        if r != 0:
            wait_status = status
            break
        time.sleep(0.1)

    if wait_status is None:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        print(f"TIMEOUT ({SCENARIO_TIMEOUT}s)")
    elif os.WIFSIGNALED(wait_status):
        sig = os.WTERMSIG(wait_status)
        nm = signal.Signals(sig).name if sig in signal.Signals._value2member_map_ else str(sig)
        print(f"CRASH ({nm})")
    elif os.WIFEXITED(wait_status) and os.WEXITSTATUS(wait_status) != 0:
        print(f"FAIL (exit {os.WEXITSTATUS(wait_status)})")
    else:
        print("OK")


def _run_scenario_threads(target_fns, thread_counts=None):
    if thread_counts is None:
        thread_counts = [THREADS] * len(target_fns)
    barrier = threading.Barrier(sum(thread_counts))
    errors = []

    def wrapper(fn):
        def wrapped():
            barrier.wait()
            try:
                fn()
            except Exception as e:
                errors.append(e)
        return wrapped

    threads = []
    for fn, count in zip(target_fns, thread_counts):
        for _ in range(count):
            threads.append(threading.Thread(target=wrapper(fn)))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
    if errors:
        print("thread errors:", errors[:3], file=sys.stderr)
        sys.exit(1)


# A wide family of shared types so many cache buckets are live at once.
NTYPES = 24
TYPES = []
for _i in range(NTYPES):
    TYPES.append(type("CT%d" % _i, (object,), {
        "m0": lambda self: 0, "m1": lambda self: 1, "m2": lambda self: 2,
        "m3": lambda self: 3, "m4": lambda self: 4, "m5": lambda self: 5,
    }))
NAMES = ["m0", "m1", "m2", "m3", "m4", "m5", "__len__", "__repr__"]


# ---------------------------------------------------------------- scenario 1
def scenario_clear_vs_lookup():
    """`sys._clear_type_cache()` (plain stores) vs `_PyType_LookupStackRef*`
    (atomic acquire loads) on the SAME cache entries."""
    def clearer():
        for _ in range(ITERATIONS):
            sys._clear_type_cache()

    def looker():
        for _ in range(ITERATIONS):
            for t in TYPES:
                for n in NAMES:
                    getattr(t, n, None)

    run_scenario(
        "type_cache_clear plain stores (:988-990) vs cache reads (:6315-6321)",
        [clearer, looker],
        [max(1, THREADS // 4), THREADS],
    )


# ---------------------------------------------------------------- scenario 2
def scenario_clear_vs_fill():
    """Clear racing the CONTRACT-abiding writer `update_cache_gil_disabled`.

    Type mutation bumps the version tag, forcing cache refills, so the
    inverse-ordered clear and the correctly-ordered store hit the same entries
    concurrently."""
    def clearer():
        for _ in range(ITERATIONS):
            sys._clear_type_cache()

    def mutator():
        for i in range(ITERATIONS):
            t = TYPES[i % NTYPES]
            setattr(t, "m%d" % (i % 6), lambda self, _i=i: _i)

    def looker():
        for _ in range(ITERATIONS):
            for t in TYPES:
                for n in NAMES:
                    getattr(t, n, None)

    run_scenario(
        "clear vs update_cache_gil_disabled (:6240) vs readers -- same entries",
        [clearer, mutator, looker],
        [max(1, THREADS // 4), max(1, THREADS // 2), THREADS],
    )


# ---------------------------------------------------------------- scenario 3
def scenario_clear_vs_instance_dispatch():
    """Same as 1, but the readers go through real method dispatch, which is
    the hot `_PyType_LookupStackRefAndVersion` path used by the eval loop."""
    insts = [t() for t in TYPES]

    def clearer():
        for _ in range(ITERATIONS):
            sys._clear_type_cache()

    def caller():
        for _ in range(ITERATIONS):
            for o in insts:
                o.m0(); o.m1(); o.m2(); o.m3(); o.m4(); o.m5()

    run_scenario(
        "type_cache_clear vs bound-method dispatch on shared instances",
        [clearer, caller],
        [max(1, THREADS // 4), THREADS],
    )


def _select(all_scenarios):
    """Allow one-scenario-per-process driving: `script.py <substring>`."""
    if len(sys.argv) > 1:
        want = sys.argv[1]
        return [f for f in all_scenarios if want in f.__name__]
    return all_scenarios


if __name__ == "__main__":
    print("TSan stress test for CPython type_cache / type_cache_clear")
    print(f"  Python: {sys.version.splitlines()[0]}")
    print(f"  TSan build: {IS_TSAN}")
    print(f"  Threads: {THREADS}, Iterations: {ITERATIONS}, shared types: {NTYPES}")
    print()
    for _sc in _select([scenario_clear_vs_lookup, scenario_clear_vs_fill, scenario_clear_vs_instance_dispatch]):
        _sc()

    print("\nDone. Check stderr for TSan warnings.")

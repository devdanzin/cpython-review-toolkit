#!/usr/bin/env python3
"""TSan stress test for CPython's type-watcher bit `PyTypeObject.tp_watched`.

Target: P2-F18 / P2-C2.

    Objects/typeobject.c:1112   PyType_Watch    type->tp_watched |=  (1 << id)   UNDER BEGIN_TYPE_LOCK()
    Objects/typeobject.c:1129   PyType_Unwatch  type->tp_watched &= ~(1 << id)   BARE, no lock, no atomic

Both are non-atomic read-modify-write on the same `unsigned char`
(Include/cpython/object.h:236).  The two in-tree callers are BOTH the tier-2
optimizer, and they use the SAME bit (TYPE_WATCHER_ID == 0):

    Python/optimizer_analysis.c:177   watch_type()            -> PyType_Watch(0, T)    [locked writer]
    Python/optimizer_bytecodes.c:1462/2215/2236                -> PyType_Watch(0, T)    [locked writer]
    Python/optimizer_analysis.c:156   type_watcher_callback() -> PyType_Unwatch(0, T)  [BARE writer]

so on a free-threaded build no third party is needed to create the race:
one thread optimizing a trace races another thread's type modification.

CONSEQUENCE (scenario 3 measures it): a LOST SET means PyType_Watch returns
success while the bit is not set.  The type is then modified, the watcher
callback never runs, `_Py_Executors_InvalidateDependency` never runs, and a
tier-2 executor keeps executing against a type assumption that no longer holds.

Run with a free-threaded TSan build of CPython:
    PYTHON_GIL=0 ./python tsan_stress_tp_watched.py 2> tsan_report.txt

Then triage:
    python <plugin_root>/scripts/parse_tsan_report.py tsan_report.txt
"""
import os
import signal
import sys
import threading
import time

THREADS = 8
ITERATIONS = 20_000
SCENARIO_TIMEOUT = 120  # seconds


def _is_tsan_build():
    try:
        import sysconfig
        return "fsanitize=thread" in (sysconfig.get_config_var("CFLAGS") or "").lower()
    except Exception:
        return False


IS_TSAN = _is_tsan_build()
if IS_TSAN:
    THREADS = min(THREADS, 4)
    ITERATIONS = min(ITERATIONS, 400)

import warnings
warnings.filterwarnings("ignore", ".*GIL.*")

import _testcapi


def run_scenario(name, target_fns, thread_counts=None):
    """Run one scenario in a forked child so a SEGV can't kill the parent."""
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
        pid_result, status = os.waitpid(pid, os.WNOHANG)
        if pid_result != 0:
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
    total = sum(thread_counts)
    barrier = threading.Barrier(total)
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


# ---------------------------------------------------------------- scenario 1
def scenario_watch_vs_unwatch():
    """ONE shared type. Locked `|=` at :1112 vs bare `&=~` at :1129.

    Distinct watcher ids so the two threads touch different BITS of the same
    BYTE -- there is no logical conflict at all, only the non-atomic RMW.
    """
    wid_a = _testcapi.add_type_watcher(3)
    wid_b = _testcapi.add_type_watcher(3)

    class Shared:
        pass

    def setter():
        for _ in range(ITERATIONS):
            _testcapi.watch_type(wid_a, Shared)

    def clearer():
        for _ in range(ITERATIONS):
            _testcapi.unwatch_type(wid_b, Shared)

    run_scenario(
        "tp_watched  locked |= (:1112) vs bare &=~ (:1129), one shared type",
        [setter, clearer],
        [THREADS // 2, THREADS // 2],
    )
    _testcapi.clear_type_watcher(wid_a)
    _testcapi.clear_type_watcher(wid_b)


# ---------------------------------------------------------------- scenario 2
def scenario_watched_bits_vs_notify():
    """The bare writer races the READER of the same byte.

    `_PyType_Modified_Unlocked` at :1212-:1214 does `if (type->tp_watched)`
    then `int bits = type->tp_watched;` and iterates -- two unsynchronized
    reads of the byte the bare `&=~` is writing.  `type_watcher_callback`
    itself calls PyType_Unwatch from INSIDE that loop in the real optimizer.
    """
    wid_a = _testcapi.add_type_watcher(3)
    wid_b = _testcapi.add_type_watcher(3)
    events = _testcapi.get_type_modified_events()

    class Shared:
        pass

    _testcapi.watch_type(wid_a, Shared)

    def modifier():
        for i in range(ITERATIONS):
            # re-arm: a lookup re-assigns a version tag so the next store
            # actually reaches the watcher loop at :1212
            getattr(Shared, "probe", None)
            _testcapi.watch_type(wid_a, Shared)
            Shared.probe = i
            if len(events) > 512:
                del events[:]

    def unwatcher():
        for _ in range(ITERATIONS):
            _testcapi.unwatch_type(wid_b, Shared)

    run_scenario(
        "tp_watched bare write (:1129) vs watcher-loop reads (:1212/:1214)",
        [modifier, unwatcher],
        [THREADS // 2, THREADS // 2],
    )
    _testcapi.clear_type_watcher(wid_a)
    _testcapi.clear_type_watcher(wid_b)


# ---------------------------------------------------------------- scenario 3
def scenario_lost_set_consequence():
    """THE CONSEQUENCE, observable with NO sanitizer.

    Models `Python/optimizer_analysis.c` exactly:
      - the "optimizer" thread arms the watcher (PyType_Watch, the locked
        writer at :1112) and then modifies the type, expecting a notification;
      - the "invalidation" thread spams PyType_Unwatch for a DIFFERENT watcher
        id (the bare writer at :1129) -- it arms nothing, so every observed
        notification must come from the optimizer's own bit.

    A round in which the modification produces NO event is a LOST SET: the
    optimizer believes the type is watched, it is not, and a later mutation
    will never invalidate the dependent executor.

    Run this on a plain FT build for the count, and on a GIL build as control.
    """
    rounds = 200 if IS_TSAN else 4000
    wid_opt = _testcapi.add_type_watcher(3)
    wid_noise = _testcapi.add_type_watcher(3)
    events = _testcapi.get_type_modified_events()

    lost = [0]
    total = [0]
    stop = threading.Event()

    holder = {}

    def noise():
        while not stop.is_set():
            t = holder.get("t")
            if t is not None:
                for _ in range(64):
                    _testcapi.unwatch_type(wid_noise, t)

    def optimizer():
        for i in range(rounds):
            t = type("W%d" % i, (object,), {})
            holder["t"] = t
            # arm, exactly as watch_type() in optimizer_analysis.c:177 does
            _testcapi.watch_type(wid_opt, t)
            del events[:]
            # modify -> _PyType_Modified_Unlocked -> watcher loop -> callback
            t.guarded = i
            total[0] += 1
            if not events:
                lost[0] += 1
        stop.set()

    threads = [threading.Thread(target=optimizer)]
    threads += [threading.Thread(target=noise, daemon=True) for _ in range(THREADS)]
    for t in threads:
        t.start()
    threads[0].join(timeout=SCENARIO_TIMEOUT)
    stop.set()
    for t in threads[1:]:
        t.join(timeout=5)

    _testcapi.clear_type_watcher(wid_opt)
    _testcapi.clear_type_watcher(wid_noise)
    print(f"  LOST-SET consequence: {lost[0]}/{total[0]} rounds where "
          f"PyType_Watch reported success but the modification produced NO "
          f"watcher notification")
    return lost[0], total[0]


def _select(all_scenarios):
    """Allow one-scenario-per-process driving: `script.py <substring>`."""
    if len(sys.argv) > 1:
        want = sys.argv[1]
        return [f for f in all_scenarios if want in f.__name__]
    return all_scenarios


if __name__ == "__main__":
    print("TSan stress test for CPython tp_watched (P2-F18 / P2-C2)")
    print(f"  Python: {sys.version.splitlines()[0]}")
    print(f"  TSan build: {IS_TSAN}")
    print(f"  Threads: {THREADS}, Iterations: {ITERATIONS}")
    print()
    for _sc in _select([scenario_watch_vs_unwatch, scenario_watched_bits_vs_notify, scenario_lost_set_consequence]):
        _sc()

    print("\nDone. Check stderr for TSan warnings.")

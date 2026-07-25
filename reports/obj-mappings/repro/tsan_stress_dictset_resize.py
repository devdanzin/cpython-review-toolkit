#!/usr/bin/env python3
"""TSan / FT stress for RESIZE-under-contention in CPython's dict and set.

Run on a free-threaded build:
    PYTHON_GIL=0 <build>/python tsan_stress_dictset_resize.py 2> tsan_report.txt

One shared container; writers drive it across the grow/shrink boundary while
readers iterate / len() / copy() / compare / view it.

What the resize invalidates (Objects/dictobject.c, Objects/setobject.c):
  * dictresize (dictobject.c:2265+) swaps mp->ma_keys and frees the old
    PyDictKeysObject -- every cached `k = d->ma_keys` / `entry_ptr` in a
    concurrent reader points into the freed block.
  * set_table_resize (setobject.c:340+) swaps so->table / so->mask and frees the
    old table -- setiter_iternext caches `entry = so->table` and `mask = so->mask`
    at :1119-1120 inside the set's critical section, but set_add_entry's restart
    loop is the documented acknowledgement that the table can move under a caller.
  * dict_copy / set_copy walk the table while another thread resizes it.
  * dict / set richcompare walks both operands.

Scenarios are sequential and each runs in a forked child.
"""

import os
import signal
import sys
import threading
import time

THREADS = 8
ITERATIONS = 20_000
SCENARIO_TIMEOUT = 180


def _is_tsan_build():
    try:
        import sysconfig
        blob = " ".join(str(sysconfig.get_config_var(v) or "")
                        for v in ("CFLAGS", "CONFIG_ARGS", "PY_CFLAGS"))
        return "sanitize=thread" in blob.lower()
    except Exception:
        return False


if _is_tsan_build():
    THREADS = min(THREADS, 4)
    ITERATIONS = min(ITERATIONS, 400)

import warnings
warnings.filterwarnings("ignore", ".*GIL.*")


def run_scenario(name, targets, counts):
    print("  %-46s" % (name + " ..."), end=" ", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        try:
            _run_threads(targets, counts)
            os._exit(0)
        except BaseException:
            import traceback
            traceback.print_exc()
            os._exit(1)

    deadline = time.monotonic() + SCENARIO_TIMEOUT
    status = None
    while time.monotonic() < deadline:
        done, st = os.waitpid(pid, os.WNOHANG)
        if done != 0:
            status = st
            break
        time.sleep(0.05)
    if status is None:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        print("TIMEOUT/HANG (%ds)" % SCENARIO_TIMEOUT)
        return "TIMEOUT"
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        try:
            nm = signal.Signals(sig).name
        except ValueError:
            nm = str(sig)
        print("CRASH (%s)" % nm)
        return nm
    code = os.WEXITSTATUS(status)
    if code:
        print("FAIL (exit %d)" % code)
        return "exit%d" % code
    print("ok")
    return "OK"


def _run_threads(targets, counts):
    barrier = threading.Barrier(sum(counts))
    errors = []

    def wrap(fn):
        def run():
            barrier.wait()
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        return run

    ts = []
    for fn, n in zip(targets, counts):
        ts.extend(threading.Thread(target=wrap(fn)) for _ in range(n))
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=SCENARIO_TIMEOUT)
    if errors:
        print("\n    per-thread errors: %r" % (errors[:3],), flush=True)
        sys.exit(1)


# --------------------------------------------------------------------------- #

def scenario_dict_resize_vs_read():
    """Writers cross dict grow/shrink thresholds; readers iterate/len/copy/compare."""
    shared = {i: i for i in range(8)}
    ref = {i: i for i in range(8)}

    def grower():
        for n in range(ITERATIONS):
            try:
                k = 1000 + (n % 512)
                shared[k] = n
                shared.pop(k, None)
            except RuntimeError:
                pass

    def bulk():
        for _ in range(ITERATIONS // 8):
            try:
                shared.update({("b%d" % i): i for i in range(64)})
                for i in range(64):
                    shared.pop("b%d" % i, None)
            except RuntimeError:
                pass

    def reader():
        for _ in range(ITERATIONS):
            try:
                len(shared)
                for _ in shared:
                    break
                shared.get(3)
                3 in shared
            except RuntimeError:
                pass

    def copier():
        for _ in range(ITERATIONS // 4):
            try:
                shared.copy()
                list(shared.items())
                shared == ref
            except RuntimeError:
                pass

    run_scenario("dict: resize vs iterate/len/copy/compare",
                 [grower, bulk, reader, copier],
                 [2, 1, 3, 2])


def scenario_set_resize_vs_read():
    """Writers cross set grow/shrink thresholds; readers iterate/len/copy/compare."""
    shared = set(range(8))
    ref = set(range(8))

    def grower():
        for n in range(ITERATIONS):
            try:
                shared.add(1000 + (n % 512))
                shared.discard(1000 + (n % 512))
            except RuntimeError:
                pass

    def bulk():
        for _ in range(ITERATIONS // 8):
            try:
                shared.update(range(5000, 5064))
                shared.difference_update(range(5000, 5064))
            except RuntimeError:
                pass

    def reader():
        for _ in range(ITERATIONS):
            try:
                len(shared)
                for _ in shared:
                    break
                3 in shared
            except RuntimeError:
                pass

    def opper():
        for _ in range(ITERATIONS // 4):
            try:
                shared.copy()
                shared & ref
                shared | ref
                shared == ref
                shared.issubset(ref)
            except RuntimeError:
                pass

    run_scenario("set: resize vs iterate/len/copy/setops",
                 [grower, bulk, reader, opper],
                 [2, 1, 3, 2])


def scenario_dict_clear_vs_iterate():
    """dict.clear() frees ma_keys under a live PyDict_Next-style walk.

    delitem_common / dict_clear swap in Py_EMPTY_KEYS and release the old keys;
    a concurrent list(d.items()) / PyDict_Next cursor is left pointing into it
    (the CPY-0115 lead, on the dict side).
    """
    shared = {i: i for i in range(64)}

    def clearer():
        for _ in range(ITERATIONS // 4):
            try:
                shared.clear()
                shared.update({i: i for i in range(64)})
            except RuntimeError:
                pass

    def walker():
        for _ in range(ITERATIONS // 4):
            try:
                list(shared.items())
                list(shared.keys())
                list(shared.values())
            except RuntimeError:
                pass

    run_scenario("dict: clear() vs list(items()) walk",
                 [clearer, walker], [3, 5])


def scenario_set_clear_vs_iterate():
    shared = set(range(64))

    def clearer():
        for _ in range(ITERATIONS // 4):
            try:
                shared.clear()
                shared.update(range(64))
            except RuntimeError:
                pass

    def walker():
        for _ in range(ITERATIONS // 4):
            try:
                list(shared)
                shared.copy()
            except RuntimeError:
                pass

    run_scenario("set: clear() vs list() walk", [clearer, walker], [3, 5])


def scenario_dict_view_vs_mutate():
    """dict views (keys/items) participate in set-like ops that walk the dict."""
    shared = {i: i for i in range(32)}
    other = set(range(32))

    def mutator():
        for n in range(ITERATIONS):
            try:
                shared[n % 64] = n
                shared.pop((n + 1) % 64, None)
            except RuntimeError:
                pass

    def viewer():
        ks = shared.keys()
        its = shared.items()
        for _ in range(ITERATIONS // 2):
            try:
                ks & other
                ks | other
                ks - other
                len(its)
                5 in ks
            except RuntimeError:
                pass

    run_scenario("dict: view set-ops vs mutation", [mutator, viewer], [3, 5])


def main():
    print("TSan/FT stress -- dict & set resize under contention")
    print("  python : %s" % sys.version.replace("\n", " "))
    print("  gil    : %s" % (getattr(sys, "_is_gil_enabled", lambda: "n/a")(),))
    print("  threads=%d iterations=%d" % (THREADS, ITERATIONS))
    print()
    scenario_dict_resize_vs_read()
    scenario_set_resize_vs_read()
    scenario_dict_clear_vs_iterate()
    scenario_set_clear_vs_iterate()
    scenario_dict_view_vs_mutate()
    print("\nDone -- check stderr for TSan warnings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

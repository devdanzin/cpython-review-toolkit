#!/usr/bin/env python3
"""TSan / FT stress: user __hash__ / __eq__ re-entering the SAME dict/set,
from the calling thread and from other threads at the same time.

Run on a free-threaded build:
    PYTHON_GIL=0 <build>/python tsan_stress_reentrant_hash_eq.py 2> tsan_report.txt

Every lookup in dictobject.c / setobject.c can run arbitrary Python.  The state
cached in a local across that call is the bug surface:

  Objects/dictobject.c:1156 compare_unicode_generic
      ep = &ep0[ix] is captured BEFORE PyObject_RichCompareBool at :1168, and the
      re-check at :1171 is `dk == mp->ma_keys && ep->me_key == startkey` -- a
      pointer-identity check that a mutation restoring the same key defeats.
      Also runs under LOCK_KEYS on a split table (that is CPY-0107).
  Objects/dictobject.c:1180+ compare_generic  -- same shape for a general table.
  Objects/setobject.c:264 set_add_entry
      has the documented `goto restart` loop -- the guarded twin -- rechecking
      `table != so->table || entry->key != startkey` after the user compare.
  Objects/setobject.c:96 set_lookkey_entry_threadsafe
      rechecks table and ep->key after the compare (FT path).

Scenarios drive re-entrancy from user __hash__ and __eq__ while OTHER threads
mutate and resize the same container.
"""

import os
import signal
import sys
import threading
import time

THREADS = 8
ITERATIONS = 2_000
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
    ITERATIONS = min(ITERATIONS, 300)

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

# One re-entrancy level is what breaks the state cached across the user compare;
# letting __eq__ re-enter without a depth cap instead produces unbounded MUTUAL
# recursion (the re-entrant insert probes the same bucket, calling __eq__ again),
# which starves the scenario -- measured: the *setup loop alone* did not finish in
# 10 minutes on debug-ft-nojit.  _once() bounds it per thread.
_depth = threading.local()


def _once():
    """True at most once per outermost user-compare, per thread."""
    if getattr(_depth, "busy", False):
        return False
    _depth.busy = True
    return True


def _done():
    _depth.busy = False


def scenario_dict_eq_reenters_dict():
    """A colliding key whose __eq__ mutates the same dict, while others mutate too."""
    shared = {}

    class Colliding:
        __slots__ = ("n",)

        def __init__(self, n):
            self.n = n

        def __hash__(self):
            # 4-way bucket collisions.  A single constant hash makes every
            # op an O(n) probe chain with a Python __eq__ per probe, which
            # starves the scenario instead of racing it (measured: 8000
            # iterations x 8 threads did not finish in 180 s).
            return self.n % 4

        def __eq__(self, other):
            # Runs from compare_generic (dictobject.c:1180) with `ep` cached.
            if _once():
                try:
                    shared[Plain(self.n)] = self.n
                    shared.pop(Plain(self.n), None)
                except RuntimeError:
                    pass
                finally:
                    _done()
            return isinstance(other, Colliding) and other.n == self.n

    class Plain:
        __slots__ = ("n",)

        def __init__(self, n):
            self.n = n

        def __hash__(self):
            return self.n % 4

        def __eq__(self, other):
            return isinstance(other, Plain) and other.n == self.n

    for i in range(8):
        shared[Colliding(i)] = i

    def reenterer():
        for n in range(ITERATIONS):
            try:
                shared[Colliding(n % 16)] = n
                shared.pop(Colliding(n % 16), None)
            except RuntimeError:
                pass

    def mutator():
        for n in range(ITERATIONS):
            shared[Plain(n % 32)] = n
            shared.pop(Plain(n % 32), None)

    def reader():
        for _ in range(ITERATIONS):
            try:
                len(shared)
                for _ in shared:
                    break
            except RuntimeError:
                pass

    run_scenario("dict: __eq__ re-enters dict + concurrent mutate",
                 [reenterer, mutator, reader], [3, 3, 2])


def scenario_set_eq_reenters_set():
    """set_add_entry's restart loop under a re-entrant __eq__ + concurrent resize."""
    shared = set()

    class Colliding:
        __slots__ = ("n",)

        def __init__(self, n):
            self.n = n

        def __hash__(self):
            return self.n % 4

        def __eq__(self, other):
            # Runs from set_add_entry (setobject.c:286) / set_lookkey with
            # `table`, `entry`, `mask` cached in locals.
            if _once():
                try:
                    shared.add(Other(self.n))
                    shared.discard(Other(self.n))
                except RuntimeError:
                    pass
                finally:
                    _done()
            return isinstance(other, Colliding) and other.n == self.n

    class Other:
        __slots__ = ("n",)

        def __init__(self, n):
            self.n = n

        def __hash__(self):
            return self.n % 4

        def __eq__(self, other):
            return isinstance(other, Other) and other.n == self.n

    for i in range(8):
        shared.add(Colliding(i))

    def reenterer():
        for n in range(ITERATIONS):
            try:
                shared.add(Colliding(n % 16))
                shared.discard(Colliding(n % 16))
            except RuntimeError:
                pass

    def resizer():
        for n in range(ITERATIONS // 4):
            shared.update(Other(i) for i in range(100, 132))
            shared.difference_update(Other(i) for i in range(100, 132))

    def reader():
        for _ in range(ITERATIONS):
            try:
                len(shared)
                for _ in shared:
                    break
            except RuntimeError:
                pass

    run_scenario("set: __eq__ re-enters set + concurrent resize",
                 [reenterer, resizer, reader], [3, 3, 2])


def scenario_hash_reenters():
    """User __hash__ mutating the container it is about to be looked up in."""
    shared_d = {}
    shared_s = set()

    class HashMutates:
        __slots__ = ("n",)

        def __init__(self, n):
            self.n = n

        def __hash__(self):
            if _once():
                try:
                    shared_d[self.n] = self.n
                    shared_s.add(self.n)
                    shared_d.pop(self.n, None)
                    shared_s.discard(self.n)
                except RuntimeError:
                    pass
                finally:
                    _done()
            return self.n % 5

        def __eq__(self, other):
            return isinstance(other, HashMutates) and other.n == self.n

    def worker():
        for n in range(ITERATIONS):
            try:
                shared_d[HashMutates(n % 24)] = n
                shared_d.pop(HashMutates(n % 24), None)
                shared_s.add(HashMutates(n % 24))
                shared_s.discard(HashMutates(n % 24))
            except RuntimeError:
                pass

    run_scenario("dict+set: __hash__ mutates the container",
                 [worker], [THREADS])


def scenario_split_substr_key():
    """CPY-0107 under contention: str-subclass keys into SPLIT dicts.

    insertdict -> _Py_dict_lookup:1385 -> LOCK_KEYS_IF_SPLIT -> user __eq__.
    Every thread owns its own instance, so they contend on ONE shared dk_mutex
    without any single thread self-deadlocking.
    """
    class C:
        pass

    instances = [C() for _ in range(THREADS)]
    for o in instances:
        o.a = 1
    dicts = [o.__dict__ for o in instances]

    class SubStr(str):
        def __hash__(self):
            return str.__hash__(self)

        def __eq__(self, other):
            return str.__eq__(self, other)

    def worker(tid):
        d = dicts[tid % len(dicts)]
        for n in range(ITERATIONS):
            try:
                d[SubStr("a")] = n
                d[SubStr("b%d" % (n % 4))] = n
                d.pop(SubStr("b%d" % (n % 4)), None)
            except (RuntimeError, KeyError):
                pass

    fns = [(lambda tid=i: worker(tid)) for i in range(THREADS)]
    run_scenario("split dict: str-subclass keys vs LOCK_KEYS",
                 fns, [1] * THREADS)


def main():
    print("TSan/FT stress -- re-entrant __hash__ / __eq__ in dict & set")
    print("  python : %s" % sys.version.replace("\n", " "))
    print("  gil    : %s" % (getattr(sys, "_is_gil_enabled", lambda: "n/a")(),))
    print("  threads=%d iterations=%d" % (THREADS, ITERATIONS))
    print()
    scenario_dict_eq_reenters_dict()
    scenario_set_eq_reenters_set()
    scenario_hash_reenters()
    scenario_split_substr_key()
    print("\nDone -- check stderr for TSan warnings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

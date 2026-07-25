#!/usr/bin/env python3
"""TSan / FT stress: SPLIT-table -> COMBINED-table transition under contention.

Run on a free-threaded build:
    PYTHON_GIL=0 <build>/python tsan_stress_split_combined.py 2> tsan_report.txt

Every instance of a plain class shares ONE PyDictKeysObject (the type's
ht_cached_keys).  Attribute stores from many threads therefore all funnel through
the same dk_mutex, and the escape hatches -- materialisation and detachment --
swap ma_keys / ma_values under concurrent readers.

Sites in the slice:
  insert_split_key                       dictobject.c:1943  (dk_mutex, DONT_DETACH;
                                                             CPY-0096 lives at :1971)
  split_keys_entry_added                 dictobject.c:242   dk_nentries++/dk_usable--
  store_instance_attr_lock_held          dictobject.c:7437  values->values[ix]
  make_dict_from_instance_attributes     dictobject.c:7340  split -> materialised
  _PyDict_DetachFromObject               dictobject.c:7369  split -> combined
  _PyObject_InitInlineValues             dictobject.c:7307  per-instance dk_usable--
  dictiter_iternext_threadsafe split arm dictobject.c:6085  reads ma_values + order

Scenario families:
  1. many threads adding NEW attribute names to instances of ONE class
     (pure insert_split_key contention, drives dk_usable to 0 and forces the
     split -> combined fallback at dictobject.c:7474)
  2. writers vs. __dict__ materialisation / vars() / __dict__ replacement
  3. writers vs. delattr (which detaches) vs. readers
  4. many threads instantiating the class while others add attributes
     (_PyObject_InitInlineValues' dk_usable-- races split_keys_entry_added)
"""

import os
import signal
import sys
import threading
import time

THREADS = 8
ITERATIONS = 6_000
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

def scenario_shared_keys_insert_storm():
    """Every thread adds NEW names to instances of the SAME class.

    All of them contend on one keys->dk_mutex inside insert_split_key and race
    split_keys_entry_added's dk_nentries++/dk_usable-- pair (dictobject.c:248-249).
    Fresh classes are minted so the 30-slot budget is re-exercised repeatedly
    rather than exhausted once.
    """
    def worker(tid):
        for r in range(ITERATIONS // 64):
            cls = classes[r % len(classes)]
            o = cls()
            for i in range(40):          # deliberately past SHARED_KEYS_MAX_SIZE
                setattr(o, "a%d_%d" % (tid, i), i)

    classes = [type("K%d" % i, (), {}) for i in range(16)]
    fns = [(lambda tid=i: worker(tid)) for i in range(THREADS)]
    run_scenario("split keys: concurrent new-attribute storm",
                 fns, [1] * THREADS)


def scenario_write_vs_materialise():
    """Writers vs. __dict__ materialisation and vars() on the SAME instances."""
    class C:
        pass

    objs = [C() for _ in range(8)]
    for o in objs:
        o.a = 1

    def writer(tid):
        for n in range(ITERATIONS):
            o = objs[n % len(objs)]
            try:
                setattr(o, "w%d" % (n % 8), n)
            except Exception:
                pass

    def materialiser():
        for n in range(ITERATIONS):
            o = objs[n % len(objs)]
            try:
                d = o.__dict__
                len(d)
                list(d)
                vars(o)
            except RuntimeError:
                pass

    fns = [(lambda tid=i: writer(tid)) for i in range(THREADS // 2)]
    run_scenario("split dict: setattr vs __dict__ materialise",
                 fns + [materialiser], [1] * (THREADS // 2) + [THREADS // 2])


def scenario_write_vs_detach():
    """delattr / __dict__ assignment force detach while writers keep storing."""
    class C:
        pass

    objs = [C() for _ in range(8)]
    for o in objs:
        o.a = 1

    def writer():
        for n in range(ITERATIONS):
            o = objs[n % len(objs)]
            try:
                setattr(o, "x%d" % (n % 6), n)
            except Exception:
                pass

    def detacher():
        for n in range(ITERATIONS):
            o = objs[n % len(objs)]
            try:
                delattr(o, "x%d" % (n % 6))
            except AttributeError:
                pass
            except RuntimeError:
                pass

    def replacer():
        for n in range(ITERATIONS // 8):
            o = objs[n % len(objs)]
            try:
                o.__dict__ = {"a": 1, "replaced": n}
            except Exception:
                pass

    run_scenario("split dict: setattr vs delattr vs __dict__=",
                 [writer, detacher, replacer], [4, 3, 1])


def scenario_instantiate_vs_insert():
    """_PyObject_InitInlineValues' dk_usable-- vs split_keys_entry_added's.

    dictobject.c:7315-7326 reads dk_usable, tests > 1, then decrements under
    LOCK_KEYS; insert_split_key decrements it too.  Every thread does both.
    """
    def worker(tid):
        for r in range(ITERATIONS // 32):
            cls = classes[r % len(classes)]
            batch = [cls() for _ in range(16)]
            for j, o in enumerate(batch):
                setattr(o, "f%d_%d" % (tid, j % 24), j)
            del batch

    classes = [type("I%d" % i, (), {}) for i in range(16)]
    fns = [(lambda tid=i: worker(tid)) for i in range(THREADS)]
    run_scenario("split keys: instantiate vs insert (dk_usable)",
                 fns, [1] * THREADS)


def scenario_iterate_split_while_growing():
    """Iterate an instance __dict__ (split arm of dictiter_iternext_threadsafe)
    while other threads push it across the split -> combined boundary."""
    class C:
        pass

    o = C()
    o.a = 1
    d = o.__dict__

    def grower():
        for n in range(ITERATIONS):
            try:
                setattr(o, "g%d" % (n % 40), n)
                if n % 40 == 39:
                    for i in range(40):
                        try:
                            delattr(o, "g%d" % i)
                        except AttributeError:
                            pass
            except Exception:
                pass

    def walker():
        for _ in range(ITERATIONS):
            try:
                list(d.items())
                len(d)
                d.get("a")
            except RuntimeError:
                pass

    run_scenario("split dict: iterate while crossing to combined",
                 [grower, walker], [3, 5])


def main():
    print("TSan/FT stress -- split-table / combined-table transitions")
    print("  python : %s" % sys.version.replace("\n", " "))
    print("  gil    : %s" % (getattr(sys, "_is_gil_enabled", lambda: "n/a")(),))
    print("  threads=%d iterations=%d" % (THREADS, ITERATIONS))
    print()
    scenario_shared_keys_insert_storm()
    scenario_write_vs_materialise()
    scenario_write_vs_detach()
    scenario_instantiate_vs_insert()
    scenario_iterate_split_while_growing()
    print("\nDone -- check stderr for TSan warnings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

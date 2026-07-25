#!/usr/bin/env python3
"""TSan / FT stress for CPython's dict and set ITERATORS -- the gh-154130 shape.

Run on a free-threaded build:
    PYTHON_GIL=0 <build>/python tsan_stress_dictset_iter.py 2> tsan_report.txt
Triage:
    python <plugin_root>/scripts/parse_tsan_report.py tsan_report.txt

Target sites (both in the obj-mappings slice):

  Objects/dictobject.c:6157  dictiter_iternext_threadsafe  `fail:` label
      fail:
          di->di_dict = NULL;    // :6158  plain store, no critical section on `di`
          Py_DECREF(d);          // :6159  plain DECREF of the iterator's owning ref
      `d` is a *borrowed* read of di->di_dict taken by the caller
      (dictiter_iternextkey:5784 / value:5907 / item:6039) behind nothing but a
      `d == NULL` test.  Two threads that both pass that test and both reach `fail:`
      both drop the same single reference.

  Objects/setobject.c:1129    setiter_iternext
          si->si_set = NULL;     // :1130
          Py_DECREF(so);         // :1131
      Identical shape.  The Py_BEGIN_CRITICAL_SECTION at :1116 locks the *set*, not
      the *iterator*, and it ends at :1127 -- two lines before the drop.

  Guarded twin: acquire_key_value / acquire_iter_result (dictobject.c:6167+) use
  _Py_TryIncrefCompare / _PyObject_IsUniquelyReferenced for exactly this hazard on
  the *element* refs; the iterator's own owning ref to the container gets neither.

Each scenario shares ONE iterator across all threads and re-arms it every round,
so the exhaustion instant -- the only moment the `fail:` label is reachable -- is
hit ROUNDS times instead of once.
"""

import os
import signal
import sys
import threading
import time

THREADS = 8
ROUNDS = 4000
SCENARIO_TIMEOUT = 120  # seconds


def _is_tsan_build():
    try:
        import sysconfig
        cflags = (sysconfig.get_config_var("CFLAGS") or "")
        cflags += " " + (sysconfig.get_config_var("CONFIG_ARGS") or "")
        return "thread" in cflags.lower() and "sanitize" in cflags.lower()
    except Exception:
        return False


if _is_tsan_build():
    THREADS = min(THREADS, 4)
    ROUNDS = min(ROUNDS, 300)

import warnings
warnings.filterwarnings("ignore", ".*GIL.*")


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #

def run_scenario(name, body):
    """Run `body()` in a forked child so a SIGSEGV/SIGABRT cannot kill the parent."""
    print("  %-46s" % (name + " ..."), end=" ", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        try:
            body()
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
    if code != 0:
        print("FAIL (exit %d)" % code)
        return "exit%d" % code
    print("ok")
    return "OK"


def hammer_shared_iterator(make_container, make_iter, per_round):
    """ONE iterator, THREADS threads, re-armed every round.

    The container is deliberately EMPTY (or near-empty), so the very first next()
    every thread makes after the barrier lands on `i >= n` -> `fail:`.  That puts
    all THREADS threads on the two-line unprotected drop

        di->di_dict = NULL;  Py_DECREF(d);        (dictobject.c:6158-6159)
        si->si_set  = NULL;  Py_DECREF(so);       (setobject.c:1130-1131)

    simultaneously, ROUNDS times.  A 4-element container instead staggers the
    threads and never lines them up on the exhaustion instant (measured: 0/1 on
    release-ft-nojit with 8 threads x 4000 rounds).
    """
    container = make_container()
    slot = [make_iter(container)]
    barrier = threading.Barrier(THREADS)
    errors = []

    def worker(tid):
        try:
            for _ in range(ROUNDS):
                if tid == 0:
                    slot[0] = make_iter(container)
                barrier.wait()
                it = slot[0]
                for _ in range(per_round):
                    try:
                        next(it)
                    except (StopIteration, RuntimeError):
                        pass
                barrier.wait()
        except threading.BrokenBarrierError:
            pass
        except Exception as exc:  # noqa: BLE001 - we want races, not argument bugs
            errors.append(exc)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=SCENARIO_TIMEOUT)
    if errors:
        print("\n    per-thread errors: %r" % (errors[:3],), flush=True)
        sys.exit(1)


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #

def empty_dict():
    return {}


def one_dict():
    return {"k": 1}


def empty_set():
    s = {1}
    s.discard(1)
    return s


def one_set():
    return {1}


class _C:
    pass


def split_dict():
    """__dict__ of an instance -- a SPLIT table.

    Drives dictiter_iternext_threadsafe's `_PyDict_HasSplitTable(d)` arm
    (dictobject.c:6085-6105), which reads d->ma_values and the order array.
    """
    o = _C()
    o.a = 1
    return o.__dict__


SCENARIOS = [
    ("dict keys iter, empty dict", empty_dict, iter, 1),
    ("dict keys iter, 1-elem dict", one_dict, iter, 2),
    ("dict values iter, empty dict", empty_dict, lambda d: iter(d.values()), 1),
    ("dict items iter, empty dict", empty_dict, lambda d: iter(d.items()), 1),
    ("dict reversed-keys iter, empty", empty_dict, lambda d: reversed(d), 1),
    ("dict reversed-items iter, empty", empty_dict, lambda d: reversed(d.items()), 1),
    ("split-dict (__dict__) keys iter", split_dict, iter, 2),
    ("set iter, empty set", empty_set, iter, 1),
    ("set iter, 1-elem set", one_set, iter, 2),
    ("frozenset iter, 1-elem", lambda: frozenset([1]), iter, 2),
]


def main():
    print("TSan/FT stress -- CPython dict & set iterators (gh-154130 shape)")
    print("  python : %s" % sys.version.replace("\n", " "))
    print("  gil    : %s" % (getattr(sys, "_is_gil_enabled", lambda: "n/a")(),))
    print("  threads=%d rounds=%d" % (THREADS, ROUNDS))
    print()
    results = {}
    for name, mk, mkit, per in SCENARIOS:
        results[name] = run_scenario(
            name, lambda mk=mk, mkit=mkit, per=per: hammer_shared_iterator(mk, mkit, per))
    print()
    print("RESULTS " + repr(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""TSan / FT stress: dict & set iterator METHODS racing the exhaustion drop.

    PYTHON_GIL=0 <build>/python tsan_stress_iter_methods.py 2> tsan_report.txt

`next(it)` drops the iterator's owning reference to the container on exhaustion,
with no critical section and no atomics:

    Objects/dictobject.c:6158   di->di_dict = NULL;   Py_DECREF(d);
    Objects/setobject.c:1130    si->si_set  = NULL;   Py_DECREF(so);

The iterator's own methods read that same field with a plain load and then
dereference / INCREF it:

    Objects/dictobject.c:5682  dictiter_len
        if (di->di_dict != NULL && di->di_used == GET_USED(di->di_dict))
        -- NULL-check and dereference are two separate loads of a field another
           thread NULLs and frees between them.  GET_USED(di->di_dict) is then a
           read of freed memory.  __length_hint__ is called by list(it), tuple(it),
           set(it), operator.length_hint(it).
    Objects/dictobject.c:6392  dictiter_reduce
        dictiterobject tmp = *di;   Py_XINCREF(tmp.di_dict);
        -- struct copy + INCREF of a pointer the other thread may already have
           taken to zero: INCREF of a freed object.
    Objects/setobject.c:1062   setiter_len          -- identical to dictiter_len
    Objects/setobject.c:1071   setiter_reduce       -- identical to dictiter_reduce

Guarded twin: dictobject.c:5683 loads di->len with FT_ATOMIC_LOAD_SSIZE_RELAXED on
the very next line -- the *counter* got atomics, the *pointer* it is guarding did
not.  That asymmetry is the finding, and it is the CPY-0061 (dequeiter_len) shape.

The container is a temporary owned ONLY by the iterator, so an over-DECREF reaches
zero immediately instead of merely perturbing a refcount.
"""

import operator
import os
import signal
import sys
import threading
import time

THREADS = 8
ROUNDS = 20_000
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
    ROUNDS = min(ROUNDS, 400)

import warnings
warnings.filterwarnings("ignore", ".*GIL.*")


def run_scenario(name, body):
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
    if code:
        print("FAIL (exit %d)" % code)
        return "exit%d" % code
    print("ok")
    return "OK"


def hammer(make_iter, method):
    """ONE iterator; half the threads advance it, half call `method` on it."""
    slot = [make_iter()]
    barrier = threading.Barrier(THREADS)

    def worker(tid):
        try:
            for _ in range(ROUNDS):
                if tid == 0:
                    slot[0] = make_iter()
                barrier.wait()
                it = slot[0]
                try:
                    if tid % 2 == 0:
                        next(it)
                    else:
                        method(it)
                except (StopIteration, RuntimeError, TypeError):
                    pass
                barrier.wait()
        except threading.BrokenBarrierError:
            pass

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=SCENARIO_TIMEOUT)


def _dict_iter():
    return iter({"a": 1})


def _set_iter():
    return iter({1})


def _items_iter():
    return iter({"a": 1}.items())


def _length_hint(it):
    operator.length_hint(it)


def _reduce(it):
    it.__reduce__()


def _listify(it):
    list(it)


def main():
    print("TSan/FT stress -- dict & set iterator methods vs exhaustion drop")
    print("  python : %s" % sys.version.replace("\n", " "))
    print("  gil    : %s" % (getattr(sys, "_is_gil_enabled", lambda: "n/a")(),))
    print("  threads=%d rounds=%d" % (THREADS, ROUNDS))
    print()
    cases = [
        ("dict iter: next vs __length_hint__", _dict_iter, _length_hint),
        ("dict iter: next vs __reduce__", _dict_iter, _reduce),
        ("dict iter: next vs list()", _dict_iter, _listify),
        ("dict items iter: next vs __length_hint__", _items_iter, _length_hint),
        ("dict items iter: next vs __reduce__", _items_iter, _reduce),
        ("set iter: next vs __length_hint__", _set_iter, _length_hint),
        ("set iter: next vs __reduce__", _set_iter, _reduce),
        ("set iter: next vs list()", _set_iter, _listify),
    ]
    results = {}
    for name, mk, meth in cases:
        results[name] = run_scenario(
            name, lambda mk=mk, meth=meth: hammer(mk, meth))
    print()
    print("RESULTS " + repr(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())

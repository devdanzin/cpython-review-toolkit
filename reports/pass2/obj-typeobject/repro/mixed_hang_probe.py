#!/usr/bin/env python3
"""Isolate the `scenario_mixed` hang: concurrent __class__ + __bases__ assignment.

Reproduced 5/6 on release-ft-nojit (PYTHON_GIL=0), 0/4 on release-gil-nojit.
faulthandler dumps every thread's Python stack when the watchdog fires, which
identifies which type-mutation operation is parked.

    PYTHON_GIL=0 ./python mixed_hang_probe.py [mode]

modes:  all (default) | bases_only | class_only | cache_only | bases_class
"""
import faulthandler
import sys
import threading
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "all"
ITERATIONS = 4000
DEADLINE = 20


class Base:
    x = 0


class Alt:
    x = 1


class Obj(Base):
    __slots__ = ()


class Obj2(Alt):
    __slots__ = ()


inst = Obj()
stop = threading.Event()


def rebase():
    for i in range(ITERATIONS):
        if stop.is_set():
            return
        try:
            Obj.__bases__ = (Alt,) if i & 1 else (Base,)
        except TypeError:
            pass


def setcls():
    for i in range(ITERATIONS):
        if stop.is_set():
            return
        try:
            inst.__class__ = Obj2 if i & 1 else Obj
        except TypeError:
            pass


def setattr_():
    for i in range(ITERATIONS):
        if stop.is_set():
            return
        Base.x = i
        Alt.x = i


def clearcache():
    for _ in range(ITERATIONS):
        if stop.is_set():
            return
        sys._clear_type_cache()


def read():
    for _ in range(ITERATIONS):
        if stop.is_set():
            return
        getattr(inst, "x", None)
        type(inst).__mro__
        Base.__subclasses__()


PLANS = {
    "all":         [(rebase, 2), (setcls, 2), (setattr_, 2), (clearcache, 1), (read, 4)],
    "bases_only":  [(rebase, 4)],
    "class_only":  [(setcls, 4)],
    "cache_only":  [(clearcache, 4)],
    "bases_class": [(rebase, 2), (setcls, 2)],
}

if __name__ == "__main__":
    print("mode=%s  %s" % (MODE, sys.version.splitlines()[0]), flush=True)
    faulthandler.dump_traceback_later(DEADLINE, exit=True)
    plan = PLANS[MODE]
    n = sum(c for _, c in plan)
    barrier = threading.Barrier(n)

    def wrap(fn):
        def go():
            barrier.wait()
            fn()
        return go

    threads = []
    for fn, count in plan:
        for _ in range(count):
            threads.append(threading.Thread(target=wrap(fn), daemon=True))
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=DEADLINE)
    stop.set()
    faulthandler.cancel_dump_traceback_later()
    print("COMPLETED in %.1fs" % (time.monotonic() - t0), flush=True)

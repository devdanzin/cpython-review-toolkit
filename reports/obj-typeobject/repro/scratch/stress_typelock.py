"""Stress the two TYPE_LOCK regions that call types_stop_world() WITHOUT
type_lock_prevent_release():

  Objects/typeobject.c:1743  type_set_abstractmethods  (__abstractmethods__ setter)
  Objects/typeobject.c:12522 PyType_Freeze

The hazard: types_stop_world() -> _PyEval_StopTheWorld -> PyMutex_Lock(&stw->mutex).
On contention that PyMutex_Lock detaches the thread (_PY_LOCK_DETACH), and
detach_thread() calls _PyCriticalSection_SuspendAll(), which UNLOCKS TYPE_LOCK.
The 5 sibling sites call type_lock_prevent_release() first (which NULLs
_cs_mutex so SuspendAll skips it); these 2 do not.

To make types_stop_world() actually block we need concurrent stop-the-world
traffic. GC and __class__ assignment both stop the world, so we run those in
parallel with abstractmethods churn.

Reports: any crash, any assertion failure, or a hang.
"""

import gc
import sys
import threading
import time
from abc import ABCMeta, abstractmethod

STOP = threading.Event()
ERRORS = []
ITERS = {"abstract": 0, "gc": 0, "setclass": 0, "lookup": 0}


def guard(fn):
    def wrapper():
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            ERRORS.append(f"{fn.__name__}: {type(exc).__name__}: {exc}")
    return wrapper


# A type hierarchy so _PyType_Modified_Unlocked has subclasses to walk.
class Base(metaclass=ABCMeta):
    @abstractmethod
    def f(self): ...


SUBS = []
cur = Base
for i in range(12):
    cur = type(f"Sub{i}", (cur,), {})
    SUBS.append(cur)


@guard
def churn_abstractmethods():
    """Drives type_set_abstractmethods -> BEGIN_TYPE_LOCK + types_stop_world."""
    targets = SUBS[:]
    while not STOP.is_set():
        for t in targets:
            t.__abstractmethods__ = frozenset({"f"})
            t.__abstractmethods__ = frozenset()
        ITERS["abstract"] += 1


@guard
def churn_stw_via_gc():
    """Independent stop-the-world traffic, to contend stw->mutex."""
    while not STOP.is_set():
        gc.collect(0)
        gc.collect(1)
        ITERS["gc"] += 1


class A:
    pass


class B:
    pass


@guard
def churn_set_class():
    """object_set_class also calls types_stop_world() -- more STM contention."""
    objs = [A() for _ in range(64)]
    for o in objs:
        o.keepalive = o  # make them non-uniquely-referenced
    while not STOP.is_set():
        for o in objs:
            o.__class__ = B
            o.__class__ = A
        ITERS["setclass"] += 1


@guard
def churn_version_tag():
    """assign_version_tag() under TYPE_LOCK -- the thread that would win the
    race during the drop window."""
    while not STOP.is_set():
        for t in SUBS:
            getattr(t, "f", None)
            t.__mro__
            probe = f"probe_{threading.get_ident()}"
            setattr(t, probe, 1)
            delattr(t, probe)
        ITERS["lookup"] += 1


def main() -> int:
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    threads = [
        threading.Thread(target=churn_abstractmethods, name="abstract"),
        threading.Thread(target=churn_abstractmethods, name="abstract2"),
        threading.Thread(target=churn_stw_via_gc, name="gc"),
        threading.Thread(target=churn_set_class, name="setclass"),
        threading.Thread(target=churn_version_tag, name="lookup"),
        threading.Thread(target=churn_version_tag, name="lookup2"),
    ]
    for t in threads:
        t.start()
    time.sleep(dur)
    STOP.set()
    for t in threads:
        t.join(timeout=20)
        if t.is_alive():
            print(f"HANG: thread {t.name} did not join", flush=True)
            return 3
    print(f"iters={ITERS}", flush=True)
    if ERRORS:
        for e in ERRORS:
            print("ERROR:", e, flush=True)
        return 2
    print("completed with no crash/assert/hang", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""STW-0002 repro attempt: set_clear_internal registered as tp_clear with no lock.

Mechanism under test
--------------------
Objects/setobject.c:2916 (PySet_Type) and :3008 (PyFrozenSet_Type) register
`set_clear_internal` DIRECTLY as tp_clear, with no critical section.  Every
other route into set_clear_internal holds the per-object critical section
(set.clear() is clinic @critical_section -> Objects/clinic/setobject.c.h:125;
set_init holds it at setobject.c:2780; set_difference_update_internal asserts
it at setobject.c:1983).

The guarded twin is the dict slot: dict_tp_clear (Objects/dictobject.c:5121)
calls PyDict_Clear, which wraps clear_lock_held in Py_BEGIN_CRITICAL_SECTION.

The only safety argument for the omission would be "the GC runs with the world
stopped".  That premise is FALSE: Python/gc_free_threading.c dispatches tp_clear
inside delete_garbage() (the `(void) clear(op);` at :1761), and delete_garbage
is called at :2176 -- fifteen lines AFTER _PyEval_StartTheWorld(interp) at
:2161.  CPython writes the false premise down explicitly at
Objects/weakrefobject.c:163 ("The world is stopped during GC in free-threaded
builds. It's safe to call this without holding the lock.").

Reachability requires a second thread holding a reference to a set that is in
the GC's unreachable worklist.  This script manufactures that: an object whose
__del__ (run by a Py_DECREF inside an EARLIER tp_clear in the same
delete_garbage loop) republishes a still-unreachable set to worker threads,
which then mutate it while the GC reaches its tp_clear.

Run:  <ft-python> stw_set_tp_clear_unlocked.py
Exit 0 = no crash observed.  SIGABRT / SIGSEGV / refcount abort = live bug.
"""

import gc
import sys
import threading
import time

PUBLISHED = []
LOCK = threading.Lock()
STOP = False
ROUNDS = 300


class Republisher:
    """__del__ runs from a Py_DECREF inside delete_garbage's tp_clear loop."""

    def __init__(self, victim):
        self.victim = victim
        self.cycle = self  # keep us in a cycle so the GC owns our teardown

    def __del__(self):
        # At this point `self.victim` may still be sitting in the GC's
        # unreachable worklist, waiting for its own tp_clear.
        try:
            with LOCK:
                PUBLISHED.append(self.victim)
        except Exception:
            pass


def hammer():
    while not STOP:
        with LOCK:
            targets = PUBLISHED[-8:]
        for s in targets:
            try:
                for i in range(64):
                    s.add(i)
                    s.discard(i)
                len(s)
                list(s)
            except (RuntimeError, TypeError):
                pass
        del targets


def make_garbage():
    victim = set(range(50))
    r = Republisher(victim)
    # cycle: victim holds a frozenset that holds r's dict indirectly
    holder = {"r": r, "v": victim}
    r.holder = holder
    holder["self"] = holder
    del r, holder, victim


def main():
    global STOP
    gc.disable()
    workers = [threading.Thread(target=hammer, daemon=True) for _ in range(6)]
    for w in workers:
        w.start()

    for rnd in range(ROUNDS):
        for _ in range(40):
            make_garbage()
        gc.collect()
        with LOCK:
            if len(PUBLISHED) > 400:
                del PUBLISHED[:200]

    STOP = True
    for w in workers:
        w.join(timeout=5.0)
    gc.enable()
    print(f"completed {ROUNDS} collect rounds, "
          f"{len(PUBLISHED)} republished sets -- no crash")
    return 0


if __name__ == "__main__":
    sys.exit(main())

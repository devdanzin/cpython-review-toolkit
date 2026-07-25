"""CPY-0127 -- set_clear_internal runs as tp_clear with the world RUNNING and no
critical section, racing a mutator on another thread.

    Objects/setobject.c:2916   PySet_Type       .tp_clear = set_clear_internal
    Objects/setobject.c:3008   PyFrozenSet_Type .tp_clear = set_clear_internal
    Objects/setobject.c:645    set_clear_internal   -- takes NO critical section

    Python/gc_free_threading.c:2161   _PyEval_StartTheWorld(interp);
    Python/gc_free_threading.c:2176   delete_garbage(state);
    Python/gc_free_threading.c:1742       gc_clear_unreachable(op);
    Python/gc_free_threading.c:1761       (void) clear(op);      <-- tp_clear

The only defence ever offered for the missing lock is "the GC stops the world".
It does not: `delete_garbage` is dispatched FIFTEEN LINES after StartTheWorld.

The hard part is not the lock, it is REACHABILITY: to race the clear you need a
second thread holding the very set the collector has decided is garbage.  Two
facts make that constructible.

  (a) `delete_garbage` clears the UNREACHABLE bit at :1742 *before* dispatching
      tp_clear at :1761, and it holds only the worklist's own reference.  The
      object is therefore alive, mutable and no longer marked unreachable for the
      whole duration of `set_clear_internal`.

  (b) Python still runs during that window.  Anything with `__del__` that was in
      the unreachable set was already finalized at :2149, so its `__del__` cannot
      fire again -- but an object CREATED BY A FINALIZER at :2149 was never in the
      unreachable set, was never finalized, and its `__del__` therefore fires from
      inside `delete_garbage`.

`subtype_clear` (Objects/typeobject.c:2680) clears the instance dict BEFORE
calling the base `tp_clear`, so putting such an object in the instance dict of a
`set` subclass parks the collector inside tp_clear, immediately before
`set_clear_internal` reads `so->table / so->fill / so->used / so->mask` with no
lock at all.

Publishing is done with `id()` + `ctypes.cast`, i.e. an integer, so the set gains
no traversable reference and `handle_resurrected_objects` (:2153) does not rescue
it -- which a plain `PUBLISHED.append(self.victim)` in a finalizer does, and which
is why the earlier attempt (`stw_set_tp_clear_unlocked.py`) came back 8/8 clean.

Crash faces expected:
  * `set_empty_to_minsize` (:636) stores `so->table = NULL` and restores it at
    :642.  A concurrent `set.add()` holding the object's critical section reads
    `so->table` -> NULL deref.
  * A concurrent `set.clear()` (critical section) and this unlocked
    `set_clear_internal` both capture the same `table`/`used` and both
    `Py_DECREF(entry->key)` -> double DECREF -> `_Py_NegativeRefcount`.
  * A concurrent resize `free_entries()` the old table the collector still holds
    -> use-after-free in the DECREF loop.

PRIOR ART -- this is the premise being falsified.  gh-130313 / PR gh-130126
("Avoid locking when clearing objects", MERGED) says in as many words:

    "This requires us to relax an assert because we clear objects after we've
     restarted the world.  But we're past the point of resurrection so no one
     else can be referring to this object."

CPython therefore already knows the world is running; the load-bearing claim is
"no one else can be referring to this object", and (a)+(b) above are how that
claim fails.

TWO ACQUISITION MODES, because the second one closes the "you forged a reference
with ctypes" objection:

  mode=ctypes    (default) the finalizer publishes `id(target)`; the hammer
                 revives it with ctypes.cast.  Deterministic, precise timing.
  mode=gcobjects the hammer calls `gc.get_objects()` -- a documented public API
                 whose free-threaded implementation *deliberately* filters
                 in-progress-GC objects:

                     Python/gc_free_threading.c:2423-2427  visit_get_objects
                       if (op->ob_gc_bits & (_PyGC_BITS_UNREACHABLE | _PyGC_BITS_FROZEN)) {
                           // Exclude unreachable objects (in-progress GC) and frozen
                           // objects from gc.get_objects() to match the default build.
                           return true;
                       }

                 That filter has a hole: `delete_garbage` clears the bit at
                 :1742 BEFORE dispatching tp_clear at :1761, so for the whole
                 duration of the clear the object is visible to
                 `gc.get_objects()` again.  No ctypes, no C API.

Usage:  <ft-python> CPY-0127_gc_tp_clear_vs_mutator.py [rounds] [nhammers] [mode]
Exit 0 = no crash observed (the script prints how many races it actually staged).
"""

import ctypes
import gc
import sys
import threading
import time

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 400
NHAMMERS = int(sys.argv[2]) if len(sys.argv) > 2 else 4
MODE = sys.argv[3] if len(sys.argv) > 3 else "ctypes"
NELEM = 3000
# How long the Ghost.__del__ parks the collector inside tp_clear, giving the
# hammers time to acquire the set and get into their loop.
PARK = 0.004

ADDR = [0]
GO = threading.Event()
STOP = False
STAGED = [0]
ACQUIRED = [0]

PAYLOAD = [object() for _ in range(NELEM)]


class MySet(set):
    """A set subclass so tp_clear is subtype_clear -> (dict clear) ->
    set_clear_internal, which puts the Ghost teardown immediately before the
    unlocked clear."""


class Ghost:
    """Created by Fin.__del__ during finalize_garbage, therefore never
    tp_finalize'd itself, therefore its __del__ runs from inside
    delete_garbage -- with the world running."""

    def __del__(self):
        STAGED[0] += 1
        GO.set()
        time.sleep(PARK)


class Fin:
    def __init__(self, target):
        self.target = target

    def __del__(self):
        # runs at gc_free_threading.c:2149, BEFORE handle_resurrected_objects
        try:
            ADDR[0] = id(self.target)
            self.target.ghost = Ghost()
        except Exception:
            pass


def make_garbage():
    s = MySet()
    s.update(PAYLOAD)
    f = Fin(s)
    s.holder = f          # cycle: s -> __dict__ -> f -> s
    del s, f


def _acquire_ctypes():
    addr = ADDR[0]
    if not addr:
        return None
    try:
        return ctypes.cast(addr, ctypes.py_object).value
    except Exception:
        return None


def _acquire_gcobjects():
    # Pure Python.  Only the set whose UNREACHABLE bit delete_garbage has already
    # cleared at gc_free_threading.c:1742 comes back from this call.
    try:
        for o in gc.get_objects():
            if type(o) is MySet:
                return o
    except Exception:
        pass
    return None


def hammer():
    local = [object() for _ in range(64)]
    acquire = _acquire_ctypes if MODE == "ctypes" else _acquire_gcobjects
    while not STOP:
        if not GO.wait(0.05):
            continue
        s = acquire()
        if s is None:
            continue
        ACQUIRED[0] += 1
        deadline = time.monotonic() + PARK * 2
        try:
            while time.monotonic() < deadline:
                for o in local:
                    s.add(o)
                len(s)
                s.clear()
                s.update(PAYLOAD)
                s.clear()
        except Exception:
            pass
        del s


def main():
    global STOP
    print("rounds=%d hammers=%d nelem=%d mode=%s gil=%s"
          % (ROUNDS, NHAMMERS, NELEM, MODE,
             getattr(sys, "_is_gil_enabled", lambda: "n/a")()), flush=True)
    gc.disable()
    hs = [threading.Thread(target=hammer, daemon=True) for _ in range(NHAMMERS)]
    for h in hs:
        h.start()

    for r in range(ROUNDS):
        GO.clear()
        ADDR[0] = 0
        make_garbage()
        gc.collect()
        if r % 100 == 0:
            print("round %d staged=%d acquired=%d"
                  % (r, STAGED[0], ACQUIRED[0]), flush=True)

    STOP = True
    GO.set()
    for h in hs:
        h.join(timeout=5.0)
    gc.enable()
    print("completed: %d rounds, %d tp_clear windows staged, %d acquisitions"
          % (ROUNDS, STAGED[0], ACQUIRED[0]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

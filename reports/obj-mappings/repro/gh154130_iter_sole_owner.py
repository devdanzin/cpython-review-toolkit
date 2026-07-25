#!/usr/bin/env python3
"""Shared dict/set iterator: the exhaustion drop is the ITERATOR'S ONLY reference.

    PYTHON_GIL=0 <build>/python gh154130_iter_sole_owner.py [kind] [rounds]

Sites:
    Objects/dictobject.c:5784 dictiter_iternextkey     PyDictObject *d = di->di_dict;   (borrowed read)
    Objects/dictobject.c:6158 dictiter_iternext_threadsafe   di->di_dict = NULL;
    Objects/dictobject.c:6159                                Py_DECREF(d);
    Objects/setobject.c:1101  setiter_iternext          PySetObject *so = si->si_set;   (borrowed read)
    Objects/setobject.c:1130                                 si->si_set = NULL;
    Objects/setobject.c:1131                                 Py_DECREF(so);

Neither the read at :5784 / :1101 nor the store+drop at :6158-6159 / :1130-1131 is
inside any critical section, and the drop is not conditional on having *won* the
NULL store.  Two threads that both observe a non-NULL container both drop the same
single reference.

The published stress (tsan_stress_dictset_iter.py) keeps the container alive in a
local, so an extra DECREF only perturbs the refcount and TSan is the only witness.
Here the container is a temporary whose ONLY reference is the iterator's, so the
second DECREF takes it to zero (then negative) and the failure is a hard one:
_Py_NegativeRefcount / SIGABRT on a debug free-threaded build, use-after-free on
release.

The guarded twin is in the same function: acquire_key_value / acquire_iter_result
(dictobject.c:6167+, :6199+) use _Py_TryIncrefCompare / _PyObject_IsUniquelyReferenced
for exactly this hazard on the *element* references.  The iterator's own owning
reference to the container gets neither.
"""

import sys
import threading

KIND = sys.argv[1] if len(sys.argv) > 1 else "dict"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 20_000
THREADS = 8

if KIND == "dict":
    def make_iter():
        return iter({})            # dict refcount 1, owned solely by the iterator
elif KIND == "dict1":
    def make_iter():
        return iter({"k": 1})
elif KIND == "values":
    def make_iter():
        return iter({}.values())
elif KIND == "items":
    def make_iter():
        return iter({}.items())
elif KIND == "set":
    def make_iter():
        s = {1}
        s.discard(1)
        return iter(s)             # set refcount 1, owned solely by the iterator
elif KIND == "set1":
    def make_iter():
        return iter({1})
else:
    raise SystemExit("kind must be dict|dict1|values|items|set|set1")


def main():
    print("kind=%s threads=%d rounds=%d gil=%s"
          % (KIND, THREADS, ROUNDS,
             getattr(sys, "_is_gil_enabled", lambda: "n/a")()), flush=True)
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
                    next(it)
                except (StopIteration, RuntimeError):
                    pass
                barrier.wait()
        except threading.BrokenBarrierError:
            pass

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    print("completed without crash", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

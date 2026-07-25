"""T3 iterator double-DECREF repro for iterobject.c / genericaliasobject.c.

Run with:  PYTHON_GIL=0 ./python t3_repro.py <case>
"""

import sys
import threading

ROUNDS = 400
NTHREADS = 4


def case_seqiter() -> None:
    """Objects/iterobject.c:52 iter_iternext -- it->it_seq = NULL; Py_DECREF(seq)."""
    bad = 0
    for _ in range(ROUNDS):
        barrier = threading.Barrier(NTHREADS)

        class S:
            def __getitem__(self, i):
                # Park every thread here *after* it has loaded `seq = it->it_seq`
                # and *before* any of them stores NULL back.
                try:
                    barrier.wait(timeout=5)
                except Exception:
                    pass
                raise IndexError

        s = S()
        it = iter(s)
        base = sys.getrefcount(s)  # main ref + iterator ref

        def worker():
            try:
                next(it)
            except StopIteration:
                pass

        ts = [threading.Thread(target=worker) for _ in range(NTHREADS)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        after = sys.getrefcount(s)
        # Iterator dropped exactly one ref => after == base - 1.
        if after < base - 1:
            bad += 1
            print(f"  UNDERFLOW: base={base} after={after} (lost {base - after})")
    print(f"seqiter: {bad}/{ROUNDS} rounds showed refcount underflow")


def case_calliter() -> None:
    """Objects/iterobject.c:223 calliter_iternext -- Py_CLEAR(it->it_callable)."""
    bad = 0
    for _ in range(ROUNDS):
        barrier = threading.Barrier(NTHREADS)

        class C:
            def __call__(self):
                try:
                    barrier.wait(timeout=5)
                except Exception:
                    pass
                return 0  # == sentinel -> exhaustion

        c = C()
        it = iter(c, 0)
        base = sys.getrefcount(c)

        def worker():
            try:
                next(it)
            except StopIteration:
                pass

        ts = [threading.Thread(target=worker) for _ in range(NTHREADS)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        after = sys.getrefcount(c)
        if after < base - 1:
            bad += 1
            print(f"  UNDERFLOW: base={base} after={after} (lost {base - after})")
    print(f"calliter: {bad}/{ROUNDS} rounds showed refcount underflow")


def case_gaiter() -> None:
    """Objects/genericaliasobject.c:938 ga_iternext -- Py_SETREF(gi->obj, NULL)."""
    bad = 0
    for _ in range(ROUNDS * 5):
        ga = list[int]
        it = iter(ga)
        base = sys.getrefcount(ga)
        barrier = threading.Barrier(NTHREADS)

        def worker():
            barrier.wait()
            for _ in range(20):
                try:
                    next(it)
                except StopIteration:
                    pass

        ts = [threading.Thread(target=worker) for _ in range(NTHREADS)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        after = sys.getrefcount(ga)
        if after < base - 1:
            bad += 1
            print(f"  UNDERFLOW: base={base} after={after}")
    print(f"gaiter: {bad}/{ROUNDS * 5} rounds showed refcount underflow")


if __name__ == "__main__":
    print("gil_enabled =", sys._is_gil_enabled())
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "seqiter"):
        case_seqiter()
    if which in ("all", "calliter"):
        case_calliter()
    if which in ("all", "gaiter"):
        case_gaiter()

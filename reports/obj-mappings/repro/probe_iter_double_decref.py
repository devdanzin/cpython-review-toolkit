"""Diagnostic: does the shared-iterator exhaustion path actually over-DECREF?

Measures sys.getrefcount(container) around a burst of concurrent next() calls on
ONE shared iterator whose container is empty (every thread's first next() lands on
`fail:` / si_set = NULL).

An over-DECREF shows up as the refcount going DOWN across the burst.
"""

import sys
import threading

THREADS = 8
ROUNDS = 200


def burst(make_container, make_iter):
    container = make_container()
    base = sys.getrefcount(container)
    slot = [make_iter(container)]
    barrier = threading.Barrier(THREADS)
    keepalive = []

    def worker(tid):
        for _ in range(ROUNDS):
            if tid == 0:
                slot[0] = make_iter(container)
            barrier.wait()
            it = slot[0]
            try:
                next(it)
            except (StopIteration, RuntimeError):
                pass
            barrier.wait()

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    keepalive.append(slot[0])
    after = sys.getrefcount(container)
    return base, after


def main():
    class C:
        pass

    def split():
        o = C()
        o.a = 1
        return o.__dict__

    cases = [
        ("empty dict", dict, iter),
        ("empty set", lambda: set(), iter),
        ("1-elem dict", lambda: {"k": 1}, iter),
        ("1-elem set", lambda: {1}, iter),
        ("split __dict__", split, iter),
        ("dict.values()", dict, lambda d: iter(d.values())),
        ("dict.items()", dict, lambda d: iter(d.items())),
    ]
    print("threads=%d rounds=%d  (%d concurrent next() per round)"
          % (THREADS, ROUNDS, THREADS))
    for name, mk, mkit in cases:
        base, after = burst(mk, mkit)
        delta = after - base
        print("  %-18s refcount %3d -> %3d   delta=%+d %s"
              % (name, base, after, delta,
                 "  <-- OVER-DECREF" if delta < 0 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

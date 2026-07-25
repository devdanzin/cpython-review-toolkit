"""Is `set iter: next vs __reduce__` slow under TSan, or actually hung?

The scenario timed out at 180 s under release-ft-nojit-tsan while its dict twin
completed.  This runs the same workload with progress printed every round, so a
stalled round is distinguishable from a merely slow one.

    PYTHON_GIL=0 <build>/python probe_setiter_reduce_timeout.py [rounds]
"""

import sys
import threading
import time

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 400
THREADS = 4


def main():
    print("threads=%d rounds=%d gil=%s"
          % (THREADS, ROUNDS, getattr(sys, "_is_gil_enabled", lambda: "n/a")()),
          flush=True)
    slot = [iter({1})]
    barrier = threading.Barrier(THREADS)
    t0 = time.monotonic()

    def worker(tid):
        try:
            for r in range(ROUNDS):
                if tid == 0:
                    slot[0] = iter({1})
                    if r % 25 == 0:
                        print("  round %4d  t=%.1fs" % (r, time.monotonic() - t0),
                              flush=True)
                barrier.wait()
                it = slot[0]
                try:
                    if tid % 2 == 0:
                        next(it)
                    else:
                        it.__reduce__()
                except (StopIteration, RuntimeError, TypeError):
                    pass
                barrier.wait()
        except threading.BrokenBarrierError:
            pass

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    print("completed in %.1fs" % (time.monotonic() - t0), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

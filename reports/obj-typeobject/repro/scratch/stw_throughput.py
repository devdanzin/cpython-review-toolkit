"""Is the stw_stress result a deadlock or an STW throughput collapse?
Measure type.__name__ = ... (a full _PyEval_StopTheWorld per assignment)
at 1, 2, 4, 8 threads."""
import sys, threading, time

def run(nthreads, seconds=2.0):
    counts = [0] * nthreads
    stop = False
    def w(idx):
        class C: pass
        i = 0
        while not stop:
            C.__name__ = "n"           # typeobject.c:1572 _PyEval_StopTheWorld
            i += 1
        counts[idx] = i
    ts = [threading.Thread(target=w, args=(i,)) for i in range(nthreads)]
    t0 = time.perf_counter()
    for t in ts: t.start()
    time.sleep(seconds)
    stop = True
    for t in ts: t.join()
    el = time.perf_counter() - t0
    total = sum(counts)
    return total, el, total / el

print(f"{'threads':>8} {'assignments':>12} {'sec':>7} {'per-sec':>12} {'per-thread':>12}",
      file=sys.stderr)
for n in (1, 2, 4, 8):
    total, el, rate = run(n)
    print(f"{n:>8} {total:>12} {el:>7.2f} {rate:>12.0f} {rate/n:>12.0f}",
          file=sys.stderr)

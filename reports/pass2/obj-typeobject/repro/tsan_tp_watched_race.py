"""Data race on PyTypeObject.tp_watched (unsigned char).

Objects/typeobject.c
  :1110-1113  PyType_Watch    BEGIN_TYPE_LOCK(); type->tp_watched |= (1<<id); END_TYPE_LOCK();
  :1129       PyType_Unwatch  type->tp_watched &= ~(1<<id);      <-- NO LOCK AT ALL
  :1212-1214  _PyType_Modified_Unlocked  reads type->tp_watched  (under TYPE_LOCK)
  :6988-6991  type_dealloc               reads type->tp_watched

Both writes are plain, non-atomic read-modify-write on the same byte; one is
guarded by TYPE_LOCK and one is not. Run under TSan on a free-threaded build.

tp_watched is not a test-only field: Python/optimizer_analysis.c:177 arms it
(PyType_Watch) and :156 disarms it (PyType_Unwatch) for the tier-2 JIT's
type-guard invalidation, so a lost SET means a modified type never invalidates
its executor.
"""

import sys
import threading

import _testcapi

NTHREADS = 8
ITERS = 4000


class C:
    pass


def main():
    wid = _testcapi.add_type_watcher(0)  # plain recording callback
    barrier = threading.Barrier(NTHREADS)

    def churn():
        barrier.wait()
        for _ in range(ITERS):
            try:
                _testcapi.watch_type(wid, C)
                _testcapi.unwatch_type(wid, C)
            except Exception:
                pass

    threads = [threading.Thread(target=churn) for _ in range(NTHREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

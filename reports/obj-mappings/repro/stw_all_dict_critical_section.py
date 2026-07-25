"""STW-0001 repro attempt: runtime-wide StopTheWorldAll + Py_BEGIN_CRITICAL_SECTION.

Mechanism under test
--------------------
Python/critical_section.c:50 and :69 bypass the per-object mutex when
    tstate->interp->stoptheworld.world_stopped
is set.  _PyEval_StopTheWorldAll() (Python/pystate.c:2531) stops the world via
    stop_the_world(&runtime->stoptheworld)
which sets runtime->stoptheworld.world_stopped -- NOT the per-interpreter flag.
So inside a runtime-wide STW region the bypass does not fire and
Py_BEGIN_CRITICAL_SECTION really calls PyMutex_Lock().

sys._current_frames() -> _PyThread_CurrentFrames (Python/pystate.c:2743) is a
runtime-wide STW region that calls PyDict_SetItem (pystate.c:2762) ->
Objects/dictobject.c PyDict_SetItem -> _PyDict_SetItem_Take2 ->
Py_BEGIN_CRITICAL_SECTION(mp).

If that mutex were ever contested by a thread parked at a safe point, the
stopping thread would block forever and could never call StartTheWorldAll ->
permanent runtime-wide deadlock.

Prediction: NO hang, because `result` in _PyThread_CurrentFrames is a dict
created locally at pystate.c:2731 that no other thread can reach, so its mutex
is uncontested.  This script exists to CONFIRM that negative rather than assert
it, and to stress the surrounding region (frame materialisation, HEAD_LOCK).

Run:  <ft-python> stw_all_dict_critical_section.py
Exit 0 = no hang observed (finding stays latent).  A hang = live deadlock.
"""

import sys
import threading
import time

STOP = False
ITERS = 4000
NTHREADS = 8


def dict_hammer():
    """Hold dict critical sections as often as possible."""
    d = {}
    n = 0
    while not STOP:
        for i in range(200):
            d[i] = i
            d.get(i)
        d.clear()
        n += 1
    return n


def deep_frames(depth):
    """Give _PyFrame_GetFrameObject something to materialise."""
    if depth == 0:
        while not STOP:
            time.sleep(0.0001)
        return
    deep_frames(depth - 1)


def main():
    global STOP
    threads = []
    for _ in range(NTHREADS):
        t = threading.Thread(target=dict_hammer, daemon=True)
        t.start()
        threads.append(t)
    for _ in range(4):
        t = threading.Thread(target=deep_frames, args=(60,), daemon=True)
        t.start()
        threads.append(t)

    time.sleep(0.2)

    t0 = time.monotonic()
    for i in range(ITERS):
        frames = sys._current_frames()
        # touch the result so the dict is really built
        assert isinstance(frames, dict)
        if i % 500 == 0:
            excs = sys._current_exceptions()
            assert isinstance(excs, dict)
    elapsed = time.monotonic() - t0

    STOP = True
    for t in threads:
        t.join(timeout=5.0)

    print(f"completed {ITERS} sys._current_frames() calls in {elapsed:.2f}s "
          f"with {NTHREADS} dict-hammering threads -- no deadlock")
    return 0


if __name__ == "__main__":
    sys.exit(main())

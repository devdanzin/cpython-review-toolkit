"""PyErr_Format reached inside a _PyEval_StopTheWorld region in instrumentation.c.

Python/instrumentation.c
  :2122 _PyEval_StopTheWorld(interp)          (monitoring_free_tool_id / clear)
  :2130 PyErr_Format(PyExc_OverflowError, "events set too many times")  IN-REGION
  :2368 _PyEval_StopTheWorld(interp)          (monitoring.set_events)
  :2369   -> _PyMonitoring_SetEvents -> check_tool:2022 PyErr_Format   IN-REGION
  :2369   -> _PyMonitoring_SetEvents:2050    PyErr_Format              IN-REGION
  :2453 _PyEval_StopTheWorld(interp)          (monitoring.set_local_events)
  :2454   -> _PyMonitoring_SetLocalEvents:2070 PyErr_Format            IN-REGION

vs. the deliberate counter-examples that move the same call out:
  :2478 _PyEval_StopTheWorld ... :2482 _PyEval_StartTheWorld
        :2483 PyErr_Format(PyExc_OverflowError, "events set too many times")
  Objects/codeobject.c:3560 _PyEval_StartTheWorld ... :3562 PyErr_NoMemory()

This script drives the check_tool:2022 path (an unregistered tool id) from the
main thread while worker threads run, on a free-threaded build.
"""

import sys
import threading
import time

import sys as _sys

stop = threading.Event()


def worker():
    x = 0
    while not stop.is_set():
        x = (x + 1) % 1000003


N_THREADS = int(sys.argv[1]) if len(sys.argv) > 1 else 8
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 20000

threads = [threading.Thread(target=worker, daemon=True) for _ in range(N_THREADS)]
for t in threads:
    t.start()

mon = _sys.monitoring
UNUSED_TOOL = 1          # not registered via use_tool_id -> check_tool fails
raised = 0
t0 = time.monotonic()
for i in range(ROUNDS):
    try:
        mon.set_events(UNUSED_TOOL, mon.events.LINE)
    except ValueError:
        raised += 1
    if time.monotonic() - t0 > 30:
        break
stop.set()
for t in threads:
    t.join(timeout=5)
print("rounds=%d ValueError raised inside the STW region=%d elapsed=%.1fs"
      % (i + 1, raised, time.monotonic() - t0))
print("SURVIVED", flush=True)

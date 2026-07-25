"""Bug 2: arraymodule.c unsynchronized lazy-init data race

Same pattern as the sre.c compile_template fix (commit 3cf68cd).
arraymodule.c:2360 does:
    if (state->array_reconstructor == NULL) {
        state->array_reconstructor = PyImport_ImportModuleAttrString(...)
    }
without atomic read/write. The module declares Py_MOD_GIL_NOT_USED.

Under free-threading, two threads can both see NULL and both call
PyImport_ImportModuleAttrString, causing a data race (and potentially
a reference leak if both succeed and one overwrites the other).

REQUIRES: Free-threaded build (~/projects/3.14_ft/python)
Expected: TSAN report (data race on state->array_reconstructor)
"""

import array
import pickle
import threading
import sys

if not getattr(sys.flags, "nogil", False) and sys._is_gil_enabled():
    print("WARNING: GIL is enabled. This race requires free-threading.")
    print("Use: ~/projects/3.14_ft/python bug2_array_lazy_init_race.py")

# array.__reduce_ex__ calls array._array_reconstructor which triggers
# the lazy import in arraymodule.c StructUnionType_paramfunc.
# Under free-threading, racing pickle.dumps(array) from multiple threads
# can hit the unsynchronized lazy-init.

errors = []
THREADS = 8
ITERATIONS = 1000


def worker():
    for _ in range(ITERATIONS):
        try:
            a = array.array('i', [1, 2, 3])
            pickle.dumps(a)
        except Exception as e:
            errors.append(e)


threads = [threading.Thread(target=worker) for _ in range(THREADS)]
for t in threads:
    t.start()
for t in threads:
    t.join()

if errors:
    print(f"ERRORS: {len(errors)} errors occurred")
    for e in errors[:5]:
        print(f"  {type(e).__name__}: {e}")
else:
    print(f"No Python-level errors in {THREADS * ITERATIONS} iterations.")
    print("The race may still exist — run under TSAN to detect it:")
    print("  TSAN_OPTIONS=... ~/projects/3.14_ft/python bug2_array_lazy_init_race.py")

"""Bug 3: _decimal.c unsynchronized lazy-init data race

Same pattern as sre.c fix (commit 3cf68cd).
_decimal.c:3492 does:
    if (state->PyDecimal == NULL) {
        state->PyDecimal = PyImport_ImportModuleAttrString("_pydecimal", "Decimal")
    }
without atomic read/write. Module declares Py_MOD_GIL_NOT_USED.

REQUIRES: Free-threaded build (~/projects/3.14_ft/python)
Expected: TSAN report (data race on state->PyDecimal)
"""

import decimal
import threading
import sys

if not getattr(sys.flags, "nogil", False) and sys._is_gil_enabled():
    print("WARNING: GIL is enabled. This race requires free-threading.")
    print("Use: ~/projects/3.14_ft/python bug3_decimal_lazy_init_race.py")

# The lazy init of state->PyDecimal is triggered by operations that
# fall back to the Python _pydecimal implementation.
# Specifically, calling Decimal.__format__ with certain format specs
# can trigger this path.

errors = []
THREADS = 8
ITERATIONS = 500


def worker():
    for _ in range(ITERATIONS):
        try:
            d = decimal.Decimal("3.14")
            # format triggers internal operations that may hit the lazy-init
            format(d, '.2f')
            # Also try operations that might trigger _pydecimal fallback
            str(d)
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
    print("Run under TSAN to detect the data race.")

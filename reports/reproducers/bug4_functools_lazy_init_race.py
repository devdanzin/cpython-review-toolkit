"""Bug 4: _functoolsmodule.c unsynchronized lazy-init + reference leak

_functoolsmodule.c:105 does:
    if (state->placeholder == NULL) {
        state->placeholder = Py_NewRef(placeholder);
    }
without atomic read/write. Module declares Py_MOD_GIL_NOT_USED.

Two threads can both pass the NULL check, both write state->placeholder,
and the first write is overwritten — leaking the first reference.

REQUIRES: Free-threaded build (~/projects/3.14_ft/python)
Expected: TSAN report (data race on state->placeholder)
"""

import functools
import threading
import sys

if hasattr(sys, "_is_gil_enabled") and sys._is_gil_enabled():
    print("WARNING: GIL is enabled. This race requires free-threading.")
    print("Use: ~/projects/3.14_ft/python bug4_functools_lazy_init_race.py")

# The lazy-init of state->placeholder is triggered by calling
# functools.Placeholder (the type itself — its tp_new triggers it).
# Under free-threading, racing calls to create Placeholder instances
# from multiple threads can hit the unsynchronized path.

errors = []
results = []
THREADS = 8
ITERATIONS = 1000


def worker():
    for _ in range(ITERATIONS):
        try:
            # Access the Placeholder type — this triggers the lazy-init
            # path in _functoolsmodule.c placeholder_new()
            p = functools.Placeholder
            results.append(id(p))
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
    unique_ids = len(set(results))
    print(f"No Python-level errors in {THREADS * ITERATIONS} iterations.")
    print(f"Unique Placeholder ids seen: {unique_ids}")
    if unique_ids > 1:
        print("MULTIPLE Placeholder objects created — ref leak confirmed!")
    else:
        print("Single Placeholder — race didn't manifest at Python level.")
    print("Run under TSAN to detect the data race.")

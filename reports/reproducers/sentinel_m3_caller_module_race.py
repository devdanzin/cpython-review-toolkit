"""M3: caller() races on func->func_module.

In Objects/sentinelobject.c:30-46, caller() does:

    PyFunctionObject *func = _PyFrame_GetFunction(f);   // borrowed
    PyObject *r = PyFunction_GetModule((PyObject *)func); // borrowed read of func->func_module
    if (!r) { ... }
    return Py_NewRef(r);                                  // <-- racy

Between the read of `func->func_module` and `Py_NewRef(r)`, another thread
can reassign `f.__module__`, dropping the previous string's refcount to 0
and freeing it. Py_NewRef(r) then increments freed memory.

PyFunction_GetModule (Objects/funcobject.c) returns the field directly with
no critical section and no atomic. The reassignment path
(func_set___module__) does Py_CLEAR(op->func_module) which decrefs the old
string — if no other reference exists, it's freed.

Demonstration on the FT build (~/projects/ft_cpython/python, GIL disabled):
- Caller thread: tight loop calling factory() which calls sentinel(name)
- Churn thread:  tight loop reassigning factory.__module__ to fresh strings

Expected: ASAN heap-use-after-free at refcount.h Py_INCREF, called from
sentinel_repr or member-descriptor read of self->module after the sentinel
captured a freed-then-reused pointer.

The race is timing-dependent. Run multiple times if it doesn't trigger.
"""
import builtins
import sys
import threading
import time


# A factory function: each call creates a sentinel; caller() captures
# this function's __module__ as the sentinel's self->module.
def factory():
    return builtins.sentinel("VICTIM")


stop = threading.Event()
caller_count = [0]
churn_count = [0]
errors = []


def caller_thread():
    """Repeatedly call factory(), exercising caller() reading func->func_module."""
    while not stop.is_set():
        try:
            s = factory()
            # Touch self->module to consume the captured reference.
            # Both `s.__module__` (atomic path) and any C-level use of
            # self->module would see a possibly-freed pointer.
            _ = s.__module__
            caller_count[0] += 1
        except Exception as e:
            errors.append(("caller", type(e).__name__, str(e)))
            return


def churn_thread(tid):
    """Continuously reassign factory.__module__, freeing the previous string.

    We construct each replacement string fresh and let it go out of scope
    immediately — only `factory.__module__` holds it. The next reassignment
    drops the function's reference and (since nothing else holds it) frees
    the string. If caller() on another thread captured a borrowed pointer
    to that string, the subsequent Py_NewRef hits freed memory.
    """
    i = 0
    while not stop.is_set():
        try:
            # Fresh, unique, long string — not interned, not retained.
            new_module = f"m_{tid}_{i}_" + "x" * 256
            factory.__module__ = new_module
            # Local var goes out of scope; only factory holds the ref now.
            del new_module
            i += 1
            churn_count[0] += 1
        except Exception as e:
            errors.append(("churn", type(e).__name__, str(e)))
            return


def main():
    # Warm up: create one sentinel before threading to ensure pdb-like
    # one-time setup is done.
    builtins.sentinel("warmup")

    callers = [threading.Thread(target=caller_thread) for _ in range(8)]
    churners = [threading.Thread(target=churn_thread, args=(i,)) for i in range(4)]

    for t in callers + churners:
        t.start()

    DURATION = 5.0
    print(f"Racing for {DURATION}s with {len(callers)} caller + {len(churners)} churn threads...")
    time.sleep(DURATION)
    stop.set()
    for t in callers + churners:
        t.join(timeout=2.0)

    print(f"caller() iterations: {caller_count[0]:,}")
    print(f"churn iterations:    {churn_count[0]:,}")
    if errors:
        print(f"Errors: {len(errors)} (first 5):")
        for kind, exc, msg in errors[:5]:
            print(f"  [{kind}] {exc}: {msg}")
    else:
        print("No Python-level errors. The race may not have manifested this run.")
        print("Run again, or under TSan, for reliable detection of the data race.")


if __name__ == "__main__":
    main()

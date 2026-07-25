"""H1: Resurrection during sentinel dealloc → use-after-free.

sentinel_dealloc (Objects/sentinelobject.c:112) does:
    _PyObject_GC_UNTRACK(op);
    sentinel_clear(op);          // Py_CLEAR(self->name) ; Py_CLEAR(self->module)
    Py_TYPE(op)->tp_free(op);    // ← unconditionally frees, even if resurrected

There's no tp_finalize. If `name` (or `module`) is a user-controlled object
whose deallocation runs Python code (e.g., a str subclass with __del__,
weakref callback, etc.), that Python code can RESURRECT the sentinel by
recovering its address (e.g., via ctypes.cast). When the resurrecting code
returns, control unwinds back to sentinel_dealloc which calls tp_free —
freeing the sentinel even though refcount > 0. Any subsequent use of the
resurrected reference is a use-after-free.

The canonical fix is `tp_finalize` + `PyObject_CallFinalizerFromDealloc()`
(see Objects/genobject.c) which checks for resurrection and bails out of
tp_free if refcount > 0.

Tested on: ~/projects/ft_cpython/python (3.15a8 free-threaded), PYTHON_GIL=1.
Expected: crash / SIGSEGV / ASAN heap-use-after-free during the gc.collect().
"""
import builtins
import ctypes
import gc
import sys

# Slot for the resurrected reference
held = []
# We capture the sentinel's address at construction time so the __del__
# of `name` can recover the still-allocated-but-being-deallocated sentinel.
sentinel_addr = [None]


class EvilName(str):
    """str subclass that resurrects the owning sentinel during its own __del__."""
    def __del__(self):
        addr = sentinel_addr[0]
        if addr is None:
            return
        sentinel_addr[0] = None  # only resurrect once
        # ctypes.cast(addr, py_object).value does Py_INCREF(self) on the target
        # at `addr`. The target is the half-cleared sentinel (refcount=0,
        # GC-untracked, name already NULL'd by Py_CLEAR, module about to be).
        # The cast resurrects refcount 0→1.
        try:
            obj = ctypes.cast(addr, ctypes.py_object).value
            held.append(obj)
            print(f"  EvilName.__del__: resurrected sentinel at 0x{addr:x}, "
                  f"refcount now {sys.getrefcount(obj) - 1}", flush=True)
        except Exception as e:
            print(f"  EvilName.__del__: resurrection failed: {e}", flush=True)


def trigger():
    name = EvilName("victim")
    s = builtins.sentinel(name)
    sentinel_addr[0] = id(s)
    # Drop our refs. `name` is held by sentinel via self->name.
    # On `del s`: refcount(s) → 0 → sentinel_dealloc runs:
    #   - _PyObject_GC_UNTRACK(s)
    #   - sentinel_clear(s):
    #       - Py_CLEAR(self->name) sets self->name = NULL, then Py_DECREF(name)
    #       - name's refcount → 0 → str dealloc → __del__ runs
    #       - __del__ resurrects s via ctypes.cast (refcount 0 → 1)
    #       - __del__ returns; name is freed
    #       - Py_CLEAR(self->module) clears module
    #   - tp_free(s) FREES s despite refcount=1 because there's no tp_finalize
    #     guard — `held[0]` is now a dangling pointer.
    del name, s


print("Phase 1: trigger resurrection-then-free")
trigger()
print(f"  held = {held!r}  (this prints the dangling reference)", flush=True)

print("Phase 2: gc.collect() will traverse all GC-tracked objects.")
print("  The dangling sentinel is reachable from `held` but already freed.")
print("  Expect a crash here, or in any subsequent attribute access.", flush=True)
gc.collect()
gc.collect()

print("Phase 3: try to actually use the dangling reference")
try:
    s = held[0]
    # Any attribute access touches freed memory:
    print(f"  s.__name__ = {s.__name__!r}", flush=True)
    print(f"  repr(s) = {s!r}", flush=True)
except SystemError as e:
    print(f"  SystemError caught: {e}")
except Exception as e:
    print(f"  Exception caught: {type(e).__name__}: {e}")

print("If we got here without a crash, the UAF didn't trigger this run.")
print("Run under ASAN for reliable detection:")
print("  ASAN_OPTIONS=detect_leaks=0 ~/projects/ft_cpython/python sentinel_h1_resurrection_uaf.py")

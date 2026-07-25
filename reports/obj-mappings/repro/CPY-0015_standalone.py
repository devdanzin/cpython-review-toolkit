# CPY-0015 standalone reproducer -- no sweep harness needed.
#
#   debug-gil-nojit  / debug-ft-nojit  -> SIGABRT (_PyObject_GC_UNTRACK assert)
#   release-gil-nojit                  -> SIGSEGV (_PyGCHead_SET_NEXT, NULL prev)
#   release-ft-nojit                   -> clean MemoryError (bit-clear untrack)
#
# dictiter_new (Objects/dictobject.c:5617) allocates with PyObject_GC_New at
# :5621, runs the fallible _PyTuple_FromPairSteal at :5644, and on failure does
# Py_DECREF(di) at :5646 -- but _PyObject_GC_TRACK(di) is not until :5653.  So
# dictiter_dealloc:5662 runs the *unchecked* _PyObject_GC_UNTRACK on an object
# the GC never saw.
#
# The live 2-tuples drain the tuple freelist: _PyTuple_FromPairSteal cannot be
# made to fail while that freelist has an entry, and arming itself refills it
# (set_nomemory is METH_VARARGS, so its own 2-element argument tuple is
# released back to the freelist the moment the hooks go live).  Dropping the
# references would hand every tuple straight back, so they must stay named.
import _testcapi

d = {"a": 1, "b": 2}
v = d.items()

# The failing-allocation index is build-dependent (the interpreter's own
# start-up allocation count differs): 28 on the GIL builds, 30 on
# debug-ft-nojit.  release-ft-nojit never crashes -- its untrack is a
# bit-clear, not a doubly-linked-list unlink.  run_oom_sweep.py finds the
# index automatically; this constant is the GIL-build shortcut (pass 30 for
# debug-ft-nojit).
import sys
_n = int(sys.argv[1]) if len(sys.argv) > 1 else 28
_testcapi.set_nomemory(_n, _n + 1)
_hold = [(k, k) for k in range(24)]
iter(v)

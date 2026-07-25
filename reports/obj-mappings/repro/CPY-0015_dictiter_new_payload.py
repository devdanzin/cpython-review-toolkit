# CPY-0015 payload -- runs ARMED, under _testcapi.set_nomemory(n, n+1).
#
# `iter(v)` on a dict_items view reaches Objects/dictobject.c:5617 dictiter_new
# with itertype == &PyDictIterItem_Type.  Its only fallible step is
# `_PyTuple_FromPairSteal(Py_None, Py_None)` at :5644; on failure it runs
# `Py_DECREF(di)` at :5646 on an object that `_PyObject_GC_TRACK(di)` at :5653
# has not reached yet, so dictiter_dealloc:5662 executes the *unchecked*
# `_PyObject_GC_UNTRACK(di)` on a never-tracked object.
#
# `_PyTuple_FromPairSteal` cannot be made to fail while the 2-tuple freelist is
# non-empty -- a freelist pop is not an allocator call, so the injected failure
# never fires.  Even arming with `_testcapi.set_nomemory(a, b)` refills it: that
# is a METH_VARARGS call, so its own 2-element argument tuple is released back
# to the freelist the moment the hooks go live.  These live throwaway pairs
# drain it from inside the armed region (they must stay referenced -- dropping
# them hands each tuple straight back).
_hold = [(_hold_k, _hold_k) for _hold_k in range(24)]
it = iter(v)

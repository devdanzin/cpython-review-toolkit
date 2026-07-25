# PASS-2 payload C: the pickle / __reduce_ex__ region (typeobject.c 7848-8406):
# reduce_newobj, _PyObject_GetState, object_getstate_default, _common_reduce,
# copyreg._slotnames caching.
r1 = dobj.__reduce_ex__(2)
r2 = sobj.__reduce_ex__(2)
r3 = mixed.__reduce_ex__(2)
r4 = dobj.__reduce_ex__(1)
r5 = dobj.__reduce__()
r6 = mixed.__reduce_ex__(5)

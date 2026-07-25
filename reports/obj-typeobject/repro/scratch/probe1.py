import gc, weakref, sys
T = type('T', (), {})
r = weakref.ref(T)
del T
print("after del, alive?", r() is not None)
gc.collect()
print("after gc.collect, alive?", r() is not None)

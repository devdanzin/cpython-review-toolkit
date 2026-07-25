"""Guarded twin: abstract_issubclass (Objects/abstract.c:2571) on the same cyclic __bases__."""
class Fake:
    pass

a = Fake()
b = Fake()
# two bases so abstract_issubclass takes the recursive branch, not the n==1 loop
a.__bases__ = (a, b)
b.__bases__ = ()

print("calling issubclass(a, int) ...", flush=True)
try:
    print(issubclass(a, int))
except RecursionError as e:
    print("RecursionError:", e, flush=True)

"""merge_class_dict (Objects/typeobject.c:7078) — cyclic __bases__ via type.__dir__."""
import sys

class Fake:
    pass

a = Fake()
a.__bases__ = (a,)          # cycle

class Meta(type):
    @property
    def __bases__(cls):
        return (a,)

class C(metaclass=Meta):
    pass

print("reachability check: C.__bases__ =", C.__bases__, flush=True)
print("calling type.__dir__(C) ...", flush=True)
r = type.__dir__(C)
print("returned", len(r), flush=True)

# Unarmed setup shared by the pass-2 payloads.
# Everything here runs BEFORE set_nomemory(), so imports / freelist priming /
# warm-up construction do not burn the injection budget.
import copyreg
import pickle
import sys
import weakref


class SBase:
    __slots__ = ("a", "b")

    def __init__(self):
        self.a = 1
        self.b = 2


class SOther:
    __slots__ = ("a", "b")


class DBase:
    def __init__(self):
        self.x = 1


class DOther:
    pass


class M1:
    pass


class M2:
    pass


class Mixed(M1, M2):
    pass


class Deep0:
    pass


def _mk(n):
    prev = (Deep0,)
    ns = {}
    for i in range(n):
        cls = type("D%d" % i, prev, {"v": i})
        ns["D%d" % i] = cls
        prev = (cls,)
    return ns


DEEP = _mk(12)
LEAF = DEEP["D11"]


class SuperBase:
    def m(self):
        return 1


class SuperMid(SuperBase):
    def m(self):
        return super().m() + 1


class SuperLeaf(SuperMid):
    def m(self):
        return super().m() + 1


sleaf = SuperLeaf()
sobj = SBase()
sobj2 = SBase()
dobj = DBase()
dobj2 = DBase()
mixed = Mixed()

# warm the caches / import machinery unarmed
_ = pickle.Pickler
_ = dobj.__reduce_ex__(2)
_ = sobj.__reduce_ex__(2)
_ = copyreg.__reduce_ex__
_ = weakref.ref(dobj)
_ = LEAF.__mro__
_ = sleaf.m()
_ = repr(Mixed.__bases__)

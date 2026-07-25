# PASS-2 payload G: a WIDE payload so the sweep has a real denominator.
# The narrow per-region payloads only expose 4-12 distinct allocation-failure
# points; this one walks every pass-2 region in one pass and teardown included.
import gc


class WA:
    __slots__ = ("p", "q")


class WB:
    __slots__ = ("p", "q")


class WC(M1, M2):
    pass


wa = WA()
wa.p = 1
wa.q = 2
wa.__class__ = WB
wa.__class__ = WA

# __class__ on managed-dict instances (materialize + detach)
d1 = DBase()
d1.extra = 7
d1.__class__ = DOther
d1.__class__ = DBase

# pickle / __reduce_ex__ region
_ = d1.__reduce_ex__(2)
_ = wa.__reduce_ex__(2)
_ = mixed.__reduce_ex__(2)
_ = d1.__reduce__()

# MRO / __bases__ region
WC.__bases__ = (M2, M1)
WC.__bases__ = (M1, M2)

# lookup cache / getattro / setattro
for nm in ("aa", "bb", "cc", "dd"):
    setattr(WC, nm, 1)
    _ = getattr(WC, nm)
    delattr(WC, nm)

# super beyond construction
_ = super(SuperMid, sleaf).m()
_ = repr(super(SuperLeaf, sleaf))

# teardown of a heap type built in this window
del WC
gc.collect()

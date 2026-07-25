# COLD setup — imports only, plus class DEFINITIONS that are never exercised.
#
# The pass-2 p2_setup.py warms every path it later tests (`_ = dobj.__reduce_ex__(2)`,
# `_ = LEAF.__mro__`, ...). Warming is correct for imports and freelists, but
# warming the *code under test* collapses the payload's allocation footprint:
# measured on debug-gil-nojit, p2_setclass performs 4 allocations and
# p2_super_lookup 5, so their "clean" sweeps cover 4 and 5 failure points.
# Everything here is deliberately cold except the imports.
import copyreg  # noqa: F401
import pickle  # noqa: F401


def _mkpair(i):
    a = type(
        "CA%d" % i, (object,), {"__slots__": ("p", "q"), "__module__": "__main__"}
    )
    b = type(
        "CB%d" % i, (object,), {"__slots__": ("p", "q"), "__module__": "__main__"}
    )
    return a, b


SLOT_PAIRS = [_mkpair(i) for i in range(8)]


def _mkdictpair(i):
    a = type("DA%d" % i, (object,), {"__module__": "__main__"})
    b = type("DB%d" % i, (object,), {"__module__": "__main__"})
    return a, b


DICT_PAIRS = [_mkdictpair(i) for i in range(8)]

# Instances built but never mutated / materialized / reduced.
SLOT_OBJS = [a() for a, _b in SLOT_PAIRS]
DICT_OBJS = [a() for a, _b in DICT_PAIRS]


class LBase:
    def m(self):
        return 1


class LMid(LBase):
    def m(self):
        return super().m() + 1


class LLeaf(LMid):
    def m(self):
        return super().m() + 1


LOBJ = LLeaf()
LOOKUP_NAMES = ["q%03d" % i for i in range(24)]

# Drain the list/tuple freelists so a recycled-clean block does not hide an
# uninitialised-member window (the CPY-0014 precondition).
_hold = [[] for _ in range(4000)]

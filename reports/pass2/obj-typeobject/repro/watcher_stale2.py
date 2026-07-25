"""Tighter version: read D.x on the very next statement after C.x = 2."""
import sys

import _testcapi

try:
    from _testinternalcapi import type_get_version
except ImportError:
    type_get_version = None


class C:
    pass


class D(C):
    pass


C.x = 1
assert C.x == 1
assert D.x == 1

observed = []


def hook(args):
    observed.append(("C.__dict__[x]", C.__dict__["x"]))
    observed.append(("C.x", C.x))
    observed.append(("D.x", D.x))
    if type_get_version:
        observed.append(("ver C", type_get_version(C), "ver D", type_get_version(D)))


wid = _testcapi.add_type_watcher(1)
_testcapi.watch_type(wid, C)
sys.unraisablehook = hook

C.x = 2
d_immediately = D.x
c_immediately = C.x
verD_after = type_get_version(D) if type_get_version else None

sys.unraisablehook = sys.__unraisablehook__
_testcapi.unwatch_type(wid, C)
_testcapi.clear_type_watcher(wid)

print("in-hook:", observed)
print("immediately after:  D.x =", d_immediately, " C.x =", c_immediately,
      " C.__dict__['x'] =", C.__dict__["x"], " verD_after =", verD_after)
print("VERDICT:", "STALE SUBCLASS READ" if d_immediately != 2 else "consistent")

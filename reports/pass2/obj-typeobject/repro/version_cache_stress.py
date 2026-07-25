"""Stress interp->types.type_version_cache with short-lived heap types under the JIT.

type_dealloc (Objects/typeobject.c:6977) never calls set_version_unlocked(type, 0),
so the slot interp->types.type_version_cache[tp_version_tag % 4096] keeps a raw
(non-owning) PyTypeObject* to the dying type.  _PyType_LookupByVersion
(typeobject.c:1382) dereferences *slot to test (*slot)->tp_version_tag.
The only thing clearing the slot is type_clear -> PyType_Modified, which runs
only when the GC breaks the type's tp_mro self-cycle.
"""
import gc
import sys

try:
    import _testcapi
    getver = _testcapi.type_get_version
except Exception:
    getver = None


class Base:
    def m(self):
        return 1


def work(o):
    t = 0
    for _ in range(50):
        t += o.m()
    return t


live = []
seen_nonzero_after_del = 0
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3000

for i in range(N):
    C = type("C%d" % i, (Base,), {"m": lambda self, i=i: i})
    o = C()
    work(o)
    if getver is not None and getver(C) == 0:
        # force a version by doing a cached lookup
        work(o)
    del o
    del C
    if i % 500 == 0:
        # deliberately do NOT collect every round: let dead types pile up in
        # the version cache before the GC gets to run type_clear on them.
        pass

print("done", N, flush=True)
gc.collect()
print("OK-END", flush=True)

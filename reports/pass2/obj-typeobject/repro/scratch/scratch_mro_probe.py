"""Probe: how many 'mro' lookups on the metaclass happen during
T.__bases__ = (...), once Meta's version-tag budget is exhausted so the
type-attribute cache can never serve them.

MAX_VERSIONS_PER_CLASS is 1000 (Objects/typeobject.c:1389); after that
should_assign_version_tag() returns false, version_tag stays 0, and
_PyType_LookupStackRefAndVersion always walks find_name_in_mro().
"""
import traceback

DEPTH = 30
log = []
trace = [False]


def chain(prefix, n):
    cur = type(prefix + '0', (), {})
    for i in range(1, n):
        cur = type('%s%d' % (prefix, i), (cur,), {})
    return cur


X = chain('X', DEPTH)
Y = chain('Y', DEPTH)
Z = chain('Z', DEPTH)


class Evil:
    def __hash__(self):
        return hash('mro')

    def __eq__(self, other):
        if trace[0]:
            st = traceback.extract_stack()
            log.append(len(log))
        return False


Meta = type('Meta', (type,), {Evil(): 1})
T = Meta('T', (X,), {})

# Burn Meta's version-tag budget so lookups are never cached.
for i in range(1100):
    setattr(Meta, 'v%d' % i, i)

trace[0] = True
T.__bases__ = (Z,)
trace[0] = False
print("evil __eq__ invocations during T.__bases__ = (Z,):", len(log))

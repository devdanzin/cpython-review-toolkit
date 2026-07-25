# PASS-2 payload A: __bases__ assignment -> mro_hierarchy recompute.
# Exercises mro_implementation_unlocked (PyMem_New to_merge, :3488),
# pmerge (PyMem_New remain, :3371), type_set_bases_unlocked and the
# type_mro_modified / lookup-cache invalidation that follows.
class NB1:
    pass


class NB2:
    pass


class Sub(M1, M2):
    pass


Sub.__bases__ = (NB1, NB2)
Sub.__bases__ = (M1, M2)
LEAF.__bases__ = (DEEP["D9"],)
Mixed.__bases__ = (M2, M1)

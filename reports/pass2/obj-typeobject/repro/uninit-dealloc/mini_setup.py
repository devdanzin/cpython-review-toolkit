class M1:
    pass


class M2:
    pass


class Warm(M1, M2):
    pass


Warm.__bases__ = (M2, M1)
Warm.__bases__ = (M1, M2)
# Drain the list freelist: PyList_New pops a recycled (already clean) list
# when one is available, which hides the uninitialised-member window.
_hold = [[] for _ in range(4000)]

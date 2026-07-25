class Evil(str):
    def __eq__(self, other):
        return True
    def __hash__(self):
        return hash(str(self))


class A:
    __slots__ = (Evil("x"),)


class B:
    __slots__ = (Evil("y"),)


# Confirm the Evil instances survive into ht_slots (i.e. _Py_Mangle returned
# the same object, so __slots__ holds a str SUBCLASS with a Python __eq__).
import sys
print("A.__slots__ types:", [type(s).__name__ for s in A.__slots__], file=sys.stderr)

a = A()
print("assigning __class__ ...", file=sys.stderr)
a.__class__ = B
print("ASSIGNED, type(a) =", type(a).__name__, file=sys.stderr)
print("a.__slots__ ->", B.__slots__, file=sys.stderr)

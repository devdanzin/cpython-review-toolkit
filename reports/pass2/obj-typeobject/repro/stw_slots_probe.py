"""Probe 1: does a str subclass survive into ht_slots, and is same_slots_added reached?

Run on any build. Prints whether __eq__ of a __slots__ entry is invoked during
`obj.__class__ = Other`.
"""
import sys

calls = []


class MyStr(str):
    def __eq__(self, other):
        calls.append((str(self), str(other)))
        return str.__eq__(self, other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return str.__hash__(self)

    def __lt__(self, other):
        return str.__lt__(self, other)


class Base:
    pass


class A(Base):
    __slots__ = (MyStr("x"),)


class B(Base):
    __slots__ = (MyStr("x"),)


sa = A.__dict__.get("__slots__")
print("A.__slots__ =", sa, "elem types:", [type(e).__name__ for e in sa])

a = A()
keep = a  # second strong ref -> NOT uniquely referenced -> STW path
print("refcount(a) =", sys.getrefcount(a))
calls.clear()
try:
    a.__class__ = B
    print("assignment OK, new class:", type(a).__name__)
except TypeError as exc:
    print("assignment TypeError:", exc)
print("MyStr.__eq__ invocations during __class__ assignment:", calls)
print("REACHED_PYTHON_IN_STW =", bool(calls))
del keep

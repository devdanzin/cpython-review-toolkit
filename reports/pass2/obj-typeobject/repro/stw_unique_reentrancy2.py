"""Probe 3b: same as probe 3, but `a` is a FUNCTION LOCAL so that
_PyObject_IsUniquelyReferenced(self) is true and object_set_class takes the
gh-145566 no-stop-the-world fast path on the free-threaded build.

If the inner `o.__class__ = C` completes instead of hanging, the fast path was
taken (the world was never stopped).
"""
import gc
import sys
import weakref

wr = None
fired = []


class MyStr(str):
    def __eq__(self, other):
        if wr is not None and not fired:
            fired.append(1)
            o = wr()
            if o is not None:
                print("  [__eq__ re-enters: o.__class__ = C]", flush=True)
                o.__class__ = C
                print("  [__eq__ inner assignment RETURNED -> fast path, world was NOT stopped]",
                      flush=True)
                del o
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


def go():
    global wr
    a = A()
    wr = weakref.ref(a)
    print("outer: a.__class__ = B", flush=True)
    a.__class__ = B
    print("outer done, type(a) =", type(a).__name__, flush=True)


before = (sys.getrefcount(A), sys.getrefcount(B), sys.getrefcount(C))
print("refcounts before (A, B, C):", before, flush=True)
go()
after = (sys.getrefcount(A), sys.getrefcount(B), sys.getrefcount(C))
print("refcounts after  (A, B, C):", after, flush=True)
print("delta            (A, B, C):", tuple(x - y for x, y in zip(after, before)), flush=True)
for _ in range(3):
    gc.collect()
print("post-gc          (A, B, C):",
      (sys.getrefcount(A), sys.getrefcount(B), sys.getrefcount(C)), flush=True)
print("A still usable:", A.__name__, A.__mro__, flush=True)
print("OK", flush=True)

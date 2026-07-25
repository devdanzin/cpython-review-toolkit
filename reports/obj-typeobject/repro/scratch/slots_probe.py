import sys, gc

print("interp:", sys.version, sys.executable)

# 1. baseline layout arithmetic
class A0: __slots__ = ()
class A1: __slots__ = ('a',)
class A8: __slots__ = tuple('s%d' % i for i in range(8))
for C in (A0, A1, A8):
    print("basicsize", C.__name__, C.__basicsize__, "itemsize", C.__itemsize__,
          "dictoffset", C.__dictoffset__)

# 2. tuple subclass with slots -- after_items path
class T2(tuple): __slots__ = ('x', 'y')
print("T2 basicsize", T2.__basicsize__, "itemsize", T2.__itemsize__)
t = T2((1, 2, 3))
t.x = 'XX'; t.y = 'YY'
print("T2 inst", t, t.x, t.y, sys.getsizeof(t))

class T3(T2): __slots__ = ('z',)
u = T3((1, 2, 3, 4, 5))
u.x = 1; u.y = 2; u.z = 3
print("T3 basicsize", T3.__basicsize__, "inst", u, u.x, u.y, u.z, sys.getsizeof(u))
gc.collect()

# 3. str subclass slot names with hostile dunders
class Hostile(str):
    def __hash__(self): return 1
    def __eq__(self, other): return True
    def __lt__(self, other):
        # run code + allocate during PyList_Sort
        [object() for _ in range(50)]
        gc.collect()
        return str.__lt__(self, other)

try:
    ns = {'__slots__': (Hostile('aa'), Hostile('bb'), Hostile('cc'))}
    H = type('H', (), ns)
    print("hostile class OK", H.__slots__, H.__basicsize__)
    h = H()
    h.aa = 1; h.bb = 2; h.cc = 3
    print("hostile inst", h.aa, h.bb, h.cc)
    gc.collect()
except Exception as e:
    print("hostile ->", type(e).__name__, e)

# 4. __slots__ as a generator that mutates state mid-iteration
def gen():
    yield 'p'
    gc.collect()
    yield 'q'
try:
    G = type('G', (), {'__slots__': gen()})
    print("gen slots", G.__slots__, G.__basicsize__)
except Exception as e:
    print("gen ->", type(e).__name__, e)

# 5. duplicate names
try:
    D = type('D', (), {'__slots__': ('d', 'd', 'd')})
    print("dup slots", D.__slots__, D.__basicsize__)
    d = D(); d.d = 7; print("dup val", d.d)
except Exception as e:
    print("dup ->", type(e).__name__, e)

# 6. large-ish __slots__: 200k names -> basicsize arithmetic
n = 200000
names = tuple('v%d' % i for i in range(n))
B = type('B', (), {'__slots__': names})
print("big basicsize", B.__basicsize__, "expected", object.__basicsize__ + n * 8)
b = B()
b.v0 = 'first'; setattr(b, 'v%d' % (n - 1), 'last')
print("big ok", b.v0, getattr(b, 'v%d' % (n - 1)))
del b, B
gc.collect()
print("DONE")

import sys, gc
class H(str):
    def __eq__(self, o): return True
    def __hash__(self): return 1

class P: pass
# same slot COUNT, different names, hostile __eq__ on the tuples' elements
S1 = type('S1', (P,), {'__slots__': (H('aaa'),)})
S3 = type('S3', (P,), {'__slots__': (H('ddd'),)})
print("S1", S1.__basicsize__, "S3", S3.__basicsize__)
o = S1(); o.aaa = ['live', 'list']
try:
    o.__class__ = S3
    print("ALLOWED: now o.ddd =", o.ddd)
except TypeError as e:
    print("refused:", e)
gc.collect()

# different count, hostile __eq__ -- must still be refused by the size check
S4 = type('S4', (P,), {'__slots__': (H('e'), H('f'))})
p = S1(); p.aaa = 1
try:
    p.__class__ = S4
    print("BAD ALLOWED count-mismatch; p.e =", p.e, "p.f =", p.f)
except TypeError as e:
    print("count mismatch refused:", e)
gc.collect()

# tuple-subclass vs plain: after_items flag differs
class TB(tuple): pass
S5 = type('S5', (TB,), {'__slots__': (H('g'),)})
q = S5((1,2,3)); q.g = 'G'
print("S5 ok", q, q.g)
try:
    q.__class__ = S1
    print("BAD: cross-base allowed")
except TypeError as e:
    print("cross-base refused:", e)
gc.collect()
print("DONE")

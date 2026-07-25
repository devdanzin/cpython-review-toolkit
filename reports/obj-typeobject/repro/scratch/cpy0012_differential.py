# CPY-0012/0013 differential applied to typeobject.c:
# can a POST-CREATION mutation of the type change the count that drives
# (a) the members-array walk and (b) any allocation size?
import sys, gc, os

print("interp:", sys.executable)

class C:
    __slots__ = ('a', 'b')

c = C(); c.a = 1; c.b = 2

# (1) rewrite __slots__ in the type dict with an absurd value
for bad in (('x',) * 100000, 2**62, -1, "not a tuple"):
    try:
        C.__slots__ = bad
    except Exception as e:
        print("set __slots__ ->", type(e).__name__, e); continue
    # force every members-array consumer: traverse (gc), clear, dealloc, getattr
    gc.collect()
    d = C(); d.a = 'A'; d.b = 'B'
    gc.collect()
    del d
    gc.collect()
    print("after __slots__ =", repr(bad)[:40], "-> survived; basicsize",
          C.__basicsize__)

# (2) the sizes themselves must be READONLY
for attr in ('__basicsize__', '__itemsize__', '__dictoffset__',
             '__weakrefoffset__', '__flags__'):
    try:
        setattr(C, attr, 2**62)
        print("WRITABLE:", attr, "!!!")
    except Exception as e:
        print("readonly", attr, "->", type(e).__name__)

# (3) structseq control: the shape that DOES work there
try:
    os.terminal_size.n_fields = 8
    print("structseq n_fields writable -> CPY-0012 shape live")
except Exception as e:
    print("structseq n_fields ->", type(e).__name__, e)

# (4) hostile __eq__ in __slots__ names during __class__ assignment
class H(str):
    def __eq__(self, o): return True
    def __hash__(self): return 1

class P: pass
class S1(P): __slots__ = ('aaa',)
class S2(P): __slots__ = ('bbb', 'ccc')
o = S1(); o.aaa = 1
try:
    o.__class__ = S2
    print("BAD: __class__ S1->S2 allowed (different slot counts)")
except TypeError as e:
    print("__class__ S1->S2 refused:", e)

class S3(P): __slots__ = ('ddd',)
o2 = S1(); o2.aaa = 'live'
try:
    o2.__class__ = S3
    print("__class__ S1->S3 allowed; ddd =", o2.ddd)
except TypeError as e:
    print("__class__ S1->S3 refused:", e)
gc.collect()
print("DONE")

import sys
class Boom(str):
    def __eq__(self, o): raise KeyboardInterrupt("from __eq__")
    def __hash__(self): return 1
class P: pass
S1 = type('S1', (P,), {'__slots__': (Boom('aaa'),)})
S3 = type('S3', (P,), {'__slots__': (Boom('ddd'),)})
o = S1(); o.aaa = 1
try:
    o.__class__ = S3
except BaseException as e:
    print("raised:", type(e).__name__, e)
    print("context:", type(e.__context__).__name__ if e.__context__ else None)
print("PyErr state clean?", sys.exc_info())

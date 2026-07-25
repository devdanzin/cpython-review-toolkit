import _testcapi, sys
class A: pass
class B: pass
class C(A): pass
N = int(sys.argv[1])
_testcapi.set_nomemory(N, N+1)
exc = None
try:
    C.__bases__ = (B,)
except BaseException as e:
    exc = "%s" % type(e).__name__
try:
    _testcapi.remove_mem_hooks()
except Exception:
    pass
inB = C in B.__subclasses__()
inA = C in A.__subclasses__()
if (not inB) or inA or exc:
    print("n=%-4d exc=%-12s C in B.__subclasses__()=%s  C in A.__subclasses__()=%s  bases=%r"
          % (N, exc, inB, inA, C.__bases__))

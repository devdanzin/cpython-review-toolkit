import _testcapi, sys, gc
class A: pass
class B: pass
class C(A): pass
N = int(sys.argv[1])
_testcapi.set_nomemory(N, N+1)
try:
    C.__bases__ = (B,)
    outcome = "NO EXCEPTION (silent success?)"
except BaseException as e:
    outcome = "%s: %s" % (type(e).__name__, e)
try:
    _testcapi.remove_mem_hooks()
except Exception:
    pass
print("n=%d -> %s ; C.__bases__=%r ; C.__mro__=%r" % (N, outcome, C.__bases__, C.__mro__))

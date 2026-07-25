"""Fork-per-injection OOM sweep over the 14-file Objects/ sample surface.
Child dies with SIGSEGV (ASAN handle_segv=0) => NULL deref on an OOM error path."""
import os, sys, signal, time, gc, weakref, _testcapi
from collections import OrderedDict
from typing import TypeVarTuple, TypeVar, Union
from string.templatelib import Template

T = TypeVar('T'); Ts = TypeVarTuple('Ts')
GA1 = dict[str, tuple[*Ts]]
GA2 = list[T]
GA3 = dict[T, list[T]]

class C:
    def m(self): pass
    p = property(lambda s: 1)

def w_ga_tvt():      return GA1[int, str]
def w_ga_simple():   return GA2[int]
def w_ga_nested():   return GA3[int]
def w_ga_repr():     return repr(dict[str, list[int]])
def w_ga_params():   return (dict[T, list[T]]).__parameters__
def w_ga_call():     return list[int]()
def w_union_new():   return int | str | bytes
def w_union_repr():  return repr(int | str | float)
def w_union_sub():   return (Union[T, int])[str]
def w_template():
    x = 1; t = t"a{x}b{x}c"; return list(t)
def w_template_cat():
    x = 1; return list(t"a{x}" + t"b{x}")
def w_weakref():
    o = C(); r = weakref.ref(o, lambda w: None); return repr(r)
def w_weakproxy():
    o = C(); p = weakref.proxy(o); return repr(p)
def w_odict():       return repr(OrderedDict(a=1, b=2))
def w_odict_upd():
    d = OrderedDict(); d.update([('a',1),('b',2)]); return repr(d)
def w_odict_copy():  return OrderedDict(a=1,b=2).copy()
def w_structseq():   return repr(os.stat('.'))
def w_descr_qn():    return C.m.__qualname__, type(C.__dict__['p']).__name__
def w_cell():
    def f():
        z = 1
        def g(): return z
        return g
    return repr(f().__closure__[0])
def w_iter():        return list(iter([1,2,3])), list(iter(lambda: 1, 1))
def w_tuple():       return tuple(x for x in range(20))
def w_func():
    def f(a, b=1, *c, **d): pass
    return f.__code__, f.__defaults__, repr(f)
import os
WORK = {k[2:]: v for k, v in sorted(globals().items()) if k.startswith("w_")}

# warm up everything once so caches/interning are primed
for name, fn in WORK.items():
    try: fn()
    except Exception as e: print("WARMUP-FAIL", name, e, file=sys.stderr)
gc.collect()

if sys.argv[1] == "--direct":
    _dname, _dn = sys.argv[2], int(sys.argv[3])
    _testcapi.set_nomemory(_dn, _dn + 1)
    try:
        print("RESULT:", WORK[_dname]())
    except MemoryError:
        print("MemoryError (clean)")
    _testcapi.remove_mem_hooks()
    sys.exit(0)

LO, HI = int(sys.argv[1]), int(sys.argv[2])
only = sys.argv[3] if len(sys.argv) > 3 else None
crashes = []
for name, fn in WORK.items():
    if only and name != only:
        continue
    for n in range(LO, HI):
        pid = os.fork()
        if pid == 0:
            try:
                _testcapi.set_nomemory(n, n + 1)
                fn()
                _testcapi.remove_mem_hooks()
                os._exit(0)
            except MemoryError: os._exit(1)
            except BaseException: os._exit(2)
        deadline = time.time() + 2.0
        st = None
        while time.time() < deadline:
            w, s = os.waitpid(pid, os.WNOHANG)
            if w == pid: st = s; break
            time.sleep(0.002)
        if st is None:
            os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)
            crashes.append((name, n, "TIMEOUT")); continue
        if os.WIFSIGNALED(st):
            crashes.append((name, n, signal.Signals(os.WTERMSIG(st)).name))
print("=== crashes ===")
for c in crashes: print(*c)
print("total crashes:", len(crashes))

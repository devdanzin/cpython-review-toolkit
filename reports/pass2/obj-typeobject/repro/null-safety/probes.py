#!/usr/bin/env python3
"""Adversarial probes against the pass-2 regions of Objects/typeobject.c.

Each probe is run in a separate subprocess so a crash localizes.
Target the NULL-deref surface: custom mro(), __class__ assignment, super
lookup, type watchers, managed static types, pickle.
"""
import subprocess
import sys
import os

BUILDS = {
    "debug-gil": os.path.expanduser("~/projects/python_build_matrix/builds/debug-gil-nojit/python"),
    "release-gil": os.path.expanduser("~/projects/python_build_matrix/builds/release-gil-nojit/python"),
    "debug-ft": os.path.expanduser("~/projects/python_build_matrix/builds/debug-ft-nojit/python"),
}

PROBES = {
# ---- R11 MRO / custom mro() -------------------------------------------
"mro_returns_self_twice": r'''
class M(type):
    def mro(cls):
        return [cls, cls, object]
class C(metaclass=M): pass
print("ok", C.__mro__)
''',

"mro_mutates_bases_during_call": r'''
class A: pass
class B: pass
state = {"n": 0}
class M(type):
    def mro(cls):
        state["n"] += 1
        if state["n"] == 1:
            try:
                cls.__bases__ = (B,)
            except Exception as e:
                pass
        return type.mro(cls)
class C(A, metaclass=M): pass
print("ok", C.__mro__)
''',

"mro_returns_incomplete_via_finalizer": r'''
import gc
class Boom:
    def __del__(self):
        gc.collect()
class M(type):
    def mro(cls):
        r = type.mro(cls)
        r.append(Boom())
        r.pop()
        return r
class C(metaclass=M): pass
print("ok", C.__mro__)
''',

"mro_result_finalizer_replaces_mro": r'''
# mro_invoke comment: tp_mro can be replaced "through a finalizer of the
# return value of mro()".  Drive that path.
holder = []
depth = [0]
class L(list):
    def __del__(self):
        for t in list(holder):
            try:
                t.__bases__ = (object,)
            except Exception:
                pass
class M(type):
    def mro(cls):
        depth[0] += 1
        if depth[0] > 4:
            return type.mro(cls)
        if cls not in holder:
            holder.append(cls)
        return L(type.mro(cls))
class C(metaclass=M): pass
print("ok", C.__mro__)
''',

"set_bases_to_incomplete": r'''
class A: pass
class C(A): pass
try:
    C.__bases__ = (int,)
except TypeError as e:
    print("ok TypeError", e)
print("ok", C.__mro__)
''',

# ---- R25 __class__ assignment -----------------------------------------
"setclass_during_del": r'''
class A:
    __slots__ = ()
class B:
    __slots__ = ()
class K:
    def __del__(self):
        try:
            o.__class__ = B
        except Exception:
            pass
o = A()
k = K()
del k
o.__class__ = B
print("ok", o.__class__)
''',

"setclass_self_referential": r'''
class A: pass
class B: pass
a = A()
a.__class__ = B
b = B()
b.__class__ = A
print("ok")
''',

"setclass_module_subtype": r'''
import types
class M1(types.ModuleType): pass
class M2(types.ModuleType): pass
m = M1("x")
m.__class__ = M2
print("ok", type(m))
''',

"setclass_slots_evil_eq": r'''
# same_slots_added -> PyObject_RichCompareBool on ht_slots, inside the
# stopped world on FT.
class Evil(str):
    def __eq__(self, other):
        return True
    def __hash__(self):
        return hash(str(self))
class A:
    __slots__ = (Evil("x"),)
class B:
    __slots__ = (Evil("y"),)
a = A()
try:
    a.__class__ = B
    print("ok assigned")
except TypeError as e:
    print("ok TypeError", e)
''',

# ---- R37 super --------------------------------------------------------
"super_reinit_during_descr_get": r'''
class D:
    def __get__(self, obj, objtype=None):
        # re-initialise the live super object in place
        try:
            super.__init__(S, int, 3)
        except Exception:
            pass
        return 42
class A:
    x = D()
class B(A): pass
b = B()
S = super(B, b)
print("ok", S.x)
''',

"super_new_then_getattr": r'''
s = super.__new__(super)
try:
    print("ok", s.foo)
except AttributeError as e:
    print("ok AttributeError")
''',

"super_new_then_init_partial": r'''
s = super.__new__(super)
try:
    s.__init__()
except Exception as e:
    print("init raised", type(e).__name__)
try:
    print(s.__repr__())
except Exception as e:
    print("repr raised", type(e).__name__)
print("ok")
''',

"super_lying_class_attr": r'''
class Fake:
    @property
    def __class__(self):
        return int
class A:
    def m(self): return 1
f = Fake()
try:
    s = super(A, f)
    print("made super")
except TypeError as e:
    print("ok TypeError")
print("ok")
''',

# ---- R6 watchers ------------------------------------------------------
"type_watcher_mutates_in_callback": r'''
import _testcapi
try:
    wid = _testcapi.add_type_watcher(lambda t: None)
except Exception as e:
    print("skip", e); raise SystemExit
class C: pass
_testcapi.watch_type(wid, C)
C.x = 1
del C.x
print("ok")
''',

# ---- R19/R21 lookup cache + setattro ----------------------------------
"setattr_dunder_storm": r'''
class C: pass
for i in range(600):
    setattr(C, "__len__", lambda s: i)
    del C.__len__
print("ok")
''',

"getattro_metaclass_descr_mutates": r'''
class Meta(type):
    @property
    def zz(cls):
        cls.__bases__ = (object,)
        return 7
class Base: pass
class C(Base, metaclass=Meta):
    pass
print("ok", C.zz)
''',

"type_dict_del_during_lookup": r'''
class Meta(type):
    def __getattribute__(cls, name):
        return type.__getattribute__(cls, name)
class C(metaclass=Meta):
    def m(self): return 1
for i in range(200):
    C.m2 = lambda s: 2
    del C.m2
print("ok")
''',

# ---- R26 pickle -------------------------------------------------------
"slotnames_shrinks_during_iteration": r'''
class Evil:
    def __getattr__(self, k):
        try:
            del type(self).__slotnames__[:]
        except Exception:
            pass
        raise AttributeError(k)
class C(Evil):
    __slots__ = ("a", "b", "c", "d", "e")
c = C()
import copyreg
copyreg._slotnames(C)
try:
    print("state", c.__getstate__())
except RuntimeError as e:
    print("ok RuntimeError", e)
except Exception as e:
    print("other", type(e).__name__, e)
''',

"slotnames_nonstring": r'''
class C:
    __slots__ = ("a",)
C.__slotnames__ = [1, 2, 3]
c = C()
try:
    print(c.__getstate__())
except TypeError as e:
    print("ok TypeError")
''',

"reduce_lying_class": r'''
class Liar:
    __slots__ = ()
    @property
    def __class__(self):
        return int
l = Liar()
try:
    print("ok", l.__reduce_ex__(2))
except Exception as e:
    print("ok", type(e).__name__, e)
''',

# ---- R20 flags / abc --------------------------------------------------
"abc_register_flag_version": r'''
import abc, collections.abc
class C(metaclass=abc.ABCMeta): pass
class D: pass
for i in range(100):
    C.register(D)
    d = D()
    isinstance(d, C)
print("ok")
''',

# ---- R3 managed static types (subinterpreters) ------------------------
"subinterp_static_types": r'''
import _interpreters as I
for i in range(5):
    iid = I.create()
    I.run_string(iid, "import _datetime; x = _datetime.date(2020,1,1); str(x)")
    I.destroy(iid)
print("ok")
''',
}


def run(build, name, code):
    p = BUILDS[build]
    try:
        r = subprocess.run([p, "-c", code], capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return "TIMEOUT", ""
    tail = (r.stdout.strip().splitlines() or [""])[-1]
    err = (r.stderr.strip().splitlines() or [""])[-1]
    return r.returncode, f"{tail} | {err}"[:180]


which = sys.argv[1:] or list(BUILDS)
for name, code in PROBES.items():
    line = f"{name:38s}"
    bad = False
    for b in which:
        rc, msg = run(b, name, code)
        flag = ""
        if rc not in (0, 1):
            flag = " <<<<< ABNORMAL"
            bad = True
        line += f" {b}={rc}{flag}"
    print(line)
    if bad:
        for b in which:
            rc, msg = run(b, name, code)
            print(f"      [{b}] rc={rc} {msg}")

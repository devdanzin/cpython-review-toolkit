"""Probe `del obj.__dict__` across object shapes.

typeobject.c:4038 subtype_setdict accepts value==NULL and forwards it to
_PyObject_SetDict (typeobject.c:4012), whose non-managed-dict path does
    _PyObject_XSetRefDelayed(dictptr, Py_NewRef(value));   /* :4032 */
Py_NewRef (not Py_XNewRef) INCREFs its argument unconditionally.
The guarded twin is PyObject_GenericSetDict (Objects/object.c:2124), which
rejects value == NULL with "cannot delete __dict__".
"""
import sys

CASES = []
def case(name):
    def deco(f):
        CASES.append((name, f))
        return f
    return deco


@case("plain instance (managed dict)")
def _():
    class C: pass
    o = C(); o.x = 1
    del o.__dict__
    return o.__dict__


@case("instance, __dict__ never materialized")
def _():
    class C: pass
    o = C()
    del o.__dict__
    return o.__dict__


@case("__slots__ + '__dict__' in slots")
def _():
    class C:
        __slots__ = ('__dict__',)
    o = C(); o.x = 1
    del o.__dict__
    return o.__dict__


@case("subclass of a C type (subclass of Exception)")
def _():
    class E(Exception): pass
    o = E(); o.x = 1
    del o.__dict__
    return o.__dict__


@case("subclass of int")
def _():
    class I(int): pass
    o = I(3); o.x = 1
    del o.__dict__
    return o.__dict__


@case("function.__dict__")
def _():
    def f(): pass
    f.x = 1
    del f.__dict__
    return f.__dict__


@case("module.__dict__")
def _():
    import types
    m = types.ModuleType("m")
    del m.__dict__
    return m.__dict__


@case("class-with-slots subclassing dict-ful C base")
def _():
    class B(dict): pass
    class C(B):
        __slots__ = ()
    o = C(); o.x = 1
    del o.__dict__
    return o.__dict__


@case("type.__dict__ (on a class)")
def _():
    class C: pass
    del C.__dict__
    return C.__dict__


@case("PyType_FromSpec heaptype w/ __dict__ : _testcapi")
def _():
    import _testcapi
    t = getattr(_testcapi, "HeapCTypeWithDict", None)
    if t is None:
        return "no HeapCTypeWithDict"
    o = t(); o.x = 1
    del o.__dict__
    return o.__dict__


@case("_testcapi HeapCTypeWithManagedDict")
def _():
    import _testcapi
    t = getattr(_testcapi, "HeapCTypeWithManagedDict", None)
    if t is None:
        return "no HeapCTypeWithManagedDict"
    o = t(); o.x = 1
    del o.__dict__
    return o.__dict__


@case("_testcapi HeapCTypeWithDict subclassed in Python")
def _():
    import _testcapi
    t = getattr(_testcapi, "HeapCTypeWithDict", None)
    if t is None:
        return "no HeapCTypeWithDict"
    class C(t): pass
    o = C(); o.x = 1
    del o.__dict__
    return o.__dict__


def main():
    if len(sys.argv) > 1:
        i = int(sys.argv[1])
        name, f = CASES[i]
        print("CASE %d %s" % (i, name), flush=True)
        try:
            print("  OK -> %.100r" % (f(),), flush=True)
        except BaseException as e:
            print("  EXC %s: %s" % (type(e).__name__, e), flush=True)
        return
    for i, (n, _f) in enumerate(CASES):
        print("%d\t%s" % (i, n))

main()

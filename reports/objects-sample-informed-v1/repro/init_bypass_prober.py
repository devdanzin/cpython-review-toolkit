"""Probe: <typename> <mode:new|subclass> <action>.  Exit 0=survived, 1=python exc, signal=crash."""
import sys, itertools, weakref, collections, types, os, time, copy
import string.templatelib as tl

def table():
    T = {}
    T['tuple']=tuple; T['tuple_iterator']=type(iter(()))
    T['GenericAlias']=types.GenericAlias; T['UnionType']=type(int|str)
    T['Template']=tl.Template; T['Interpolation']=tl.Interpolation
    T['template_iter']=type(iter(tl.Template()))
    T['property']=property; T['mappingproxy']=type(type.__dict__)
    T['method_descriptor']=type(list.append)
    T['classmethod_descriptor']=type(dict.__dict__['fromkeys'])
    T['getset_descriptor']=type(type.__dict__['__dict__'])
    T['member_descriptor']=type(type(lambda:0).__dict__['__globals__'])
    T['wrapper_descriptor']=type(object.__init__)
    T['method_wrapper']=type(object().__str__)
    T['OrderedDict']=collections.OrderedDict
    T['odict_iterator']=type(iter(collections.OrderedDict()))
    T['odict_keys']=type(collections.OrderedDict().keys())
    T['odict_items']=type(collections.OrderedDict().items())
    T['odict_values']=type(collections.OrderedDict().values())
    T['function']=types.FunctionType; T['classmethod']=classmethod; T['staticmethod']=staticmethod
    T['weakref_ref']=weakref.ref; T['weakref_proxy']=weakref.ProxyType
    T['weakref_callableproxy']=weakref.CallableProxyType
    T['stat_result']=os.stat_result; T['version_info']=type(sys.version_info)
    T['struct_time']=time.struct_time
    T['seq_iterator']=type(iter([1,2])); T['callable_iterator']=type(iter(lambda:0,0))
    T['cell']=types.CellType
    return T

name, mode, action = sys.argv[1], sys.argv[2], sys.argv[3]
Tp = table()[name]
if mode == 'new':
    o = Tp.__new__(Tp)
else:
    class Sub(Tp):
        def __init__(self, *a, **k): pass
        def __new__(cls, *a, **k):
            try: return Tp.__new__(cls)
            except TypeError: return object.__new__(cls)
    o = Sub()
print("ALLOC-OK", flush=True)
A = {
 'repr': lambda: repr(o), 'str': lambda: str(o), 'hash': lambda: hash(o),
 'iter': lambda: list(itertools.islice(iter(o),5)), 'call': lambda: o(),
 'len': lambda: len(o), 'get': lambda: o.__get__(object(), object),
 'set': lambda: o.__set__(object(),1), 'delete': lambda: o.__delete__(object()),
 'eq': lambda: o==o, 'lt': lambda: o<o, 'getitem': lambda: o[0],
 'reduce': lambda: o.__reduce__(), 'copy': lambda: copy.copy(o),
 'deepcopy': lambda: copy.deepcopy(o), 'bool': lambda: bool(o),
 'next': lambda: next(o), 'sizeof': lambda: o.__sizeof__(),
 'keys': lambda: list(o.keys()), 'contains': lambda: 1 in o,
 'setattr': lambda: setattr(o,'x',1), 'index': lambda: o.index(1),
 'format': lambda: format(o), 'dir': lambda: dir(o),
 'gc': lambda: (__import__('gc').collect(), __import__('gc').get_referents(o)),
 'del': lambda: (globals().__setitem__('o', None)),
}
if action == 'getattr-all':
    for n in dir(Tp):
        try: getattr(o, n)
        except Exception: pass
elif action == 'callattr-all':
    for n in dir(Tp):
        try: m = getattr(o, n)
        except Exception: continue
        if callable(m):
            for args in ((),(o,),(0,)):
                try: m(*args)
                except Exception: pass
else:
    A[action]()
print("SURVIVED")

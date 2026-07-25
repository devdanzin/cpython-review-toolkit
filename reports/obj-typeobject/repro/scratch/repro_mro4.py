"""UAF: type_ready_inherit holds a borrowed tp_mro across overrides_hash().

Objects/typeobject.c (3.16.0a0 @ 4f3be1b5777):
  9332  PyObject *mro = lookup_tp_mro(type);        <- borrowed, no INCREF
  9335  for (i = 1; i < n; i++)
  9336      PyObject *b = PyTuple_GET_ITEM(mro, i);  <- use after free
  9338      inherit_slots(type, b)
              -> overrides_hash(type)  (:8964)
                 -> PyDict_Contains(tp_dict, '__eq__')  (:8814)
                    -> user __eq__ (non-str key in the class dict)
                       -> X.__bases__ = ...  -> mro_internal -> the old mro
                          tuple loses its last reference and is freed.

The MRO is made longer than PyTuple_MAXSAVESIZE (20) so the freed tuple does
not go through the tuple freelist and ASan can see the poisoned block.
"""
import types

DEPTH = 30

cell = types.CellType()

ns = {'__slots__': ()}
chain = [type('C0', (), dict(ns))]
for i in range(1, DEPTH):
    chain.append(type('C%d' % i, (chain[-1],), dict(ns)))
Deep = chain[-1]


class B:
    __slots__ = ()


hits = []


class Evil:
    def __hash__(self):
        return hash('__eq__')

    def __eq__(self, other):
        hits.append(other)
        if len(hits) == 1:
            X = cell.cell_contents
            n_old = len(X.__mro__)
            try:
                X.__bases__ = (Deep,)
            except Exception as e:
                print("  __bases__ failed: %r" % (e,), flush=True)
            else:
                print("  __bases__ reassigned (mro %d -> %d)"
                      % (n_old, len(X.__mro__)), flush=True)
        return False


ns2 = {'__classcell__': cell, Evil(): 1}
print("creating X ...", flush=True)
X = type('X', (Deep, B), ns2)
print("created:", X, len(X.__mro__), flush=True)

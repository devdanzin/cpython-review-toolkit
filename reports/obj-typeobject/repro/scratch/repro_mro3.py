"""type_ready_inherit holds a borrowed tp_mro across overrides_hash().

  Objects/typeobject.c:9332  mro = lookup_tp_mro(type)      <- borrowed
  Objects/typeobject.c:9338  inherit_slots(type, b)
                               -> overrides_hash(type)
                                  -> PyDict_Contains(dict, '__eq__')
                                     -> user __eq__  (non-str key in the class dict)
                                        -> X.__bases__ = ...  frees the old mro tuple
  Objects/typeobject.c:9336  PyTuple_GET_ITEM(mro, i)       <- use after free
"""
import types

cell = types.CellType()


class A:
    __slots__ = ()


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
            old = X.__mro__
            try:
                X.__bases__ = (A,)
            except Exception as e:
                print("  __bases__ failed: %r" % (e,), flush=True)
            else:
                print("  __bases__ reassigned (old mro len %d -> new %d)"
                      % (len(old), len(X.__mro__)), flush=True)
            del old
        return False


ns = {'__classcell__': cell, Evil(): 1}
print("creating X ...", flush=True)
X = type('X', (A, B), ns)
print("created:", X, X.__mro__, flush=True)

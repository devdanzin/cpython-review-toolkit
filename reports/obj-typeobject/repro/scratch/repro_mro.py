"""type_ready_inherit holds a borrowed tp_mro across overrides_hash()."""
import sys
import types

cell = types.CellType()


class A:
    pass


class B:
    pass


hits = []


class Evil:
    def __hash__(self):
        return hash('__eq__')

    def __eq__(self, other):
        hits.append(other)
        print("Evil.__eq__ hit #%d vs %r" % (len(hits), other), flush=True)
        if len(hits) == 1:
            X = cell.cell_contents
            print("  cell contents: %r" % (X,), flush=True)
            try:
                X.__bases__ = (B,)
                print("  __bases__ reassigned; new mro id=%x" % id(X.__mro__),
                      flush=True)
            except Exception as e:
                print("  __bases__ failed: %r" % (e,), flush=True)
        return False


ns = {'__classcell__': cell, Evil(): 1}
print("creating X ...", flush=True)
X = type('X', (A, B), ns)
print("created:", X, X.__mro__, flush=True)

"""Reachability of the _PyType_SetFlagsRecursive stop-the-world subclass walk."""
import collections.abc as cabc
import sys

# Build a subclass tree: root + N children, each with M grandchildren.
class Root:
    def __len__(self): return 0
    def __getitem__(self, i): raise IndexError

kids = []
for i in range(200):
    k = type(f"K{i}", (Root,), {})
    kids.append(k)
    for j in range(20):
        type(f"K{i}_{j}", (k,), {})

print("tree nodes:", 1 + len(kids) + len(kids) * 20, file=sys.stderr)
print("Root flags SEQUENCE before:",
      bool(Root.__flags__ & (1 << 5)), file=sys.stderr)

# _abc_register -> _PyType_SetFlagsRecursive -> types_stop_world()
#                -> set_flags_recursive -> _PyType_GetSubclasses (PyList_New +
#                   PyList_Append per node) all with the world stopped.
cabc.Sequence.register(Root)

SEQ = 1 << 5
print("Root  SEQUENCE after:", bool(Root.__flags__ & SEQ), file=sys.stderr)
print("K0    SEQUENCE after:", bool(kids[0].__flags__ & SEQ), file=sys.stderr)
print("K199  SEQUENCE after:", bool(kids[199].__flags__ & SEQ), file=sys.stderr)
print("match works:", file=sys.stderr)
match Root():
    case [*_]:
        print("  matched as sequence", file=sys.stderr)
    case _:
        print("  NOT matched as sequence", file=sys.stderr)

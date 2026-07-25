"""OOM inside the stop-the-world subclass walk: swallowed MemoryError +
partially-applied flags.  set_flags_recursive() (typeobject.c:6494) drops the
_PyType_GetSubclasses failure on the floor, and _PyType_SetFlagsRecursive is
void, so the caller never learns."""
import collections.abc as cabc
import sys
import _testcapi

SEQ = 1 << 5

def build():
    class Root:
        def __len__(self): return 0
        def __getitem__(self, i): raise IndexError
    kids = [type(f"K{i}", (Root,), {}) for i in range(30)]
    return Root, kids

start = int(sys.argv[1])
Root, kids = build()
before = [bool(k.__flags__ & SEQ) for k in kids]
_testcapi.set_nomemory(start, start + 1)
exc = None
try:
    cabc.Sequence.register(Root)
except BaseException as e:
    exc = e
_testcapi.remove_mem_hooks()
after = [bool(k.__flags__ & SEQ) for k in kids]
root_set = bool(Root.__flags__ & SEQ)
n_set = sum(after)
print(f"start={start} exc={type(exc).__name__ if exc else None} "
      f"root_SEQ={root_set} kids_SEQ={n_set}/{len(kids)} "
      f"pending={sys.exc_info()[0]}", file=sys.stderr)
if root_set and 0 < n_set < len(kids):
    print("  *** PARTIALLY APPLIED ***", file=sys.stderr)
if exc is None and root_set and n_set != len(kids):
    print("  *** SILENT PARTIAL APPLICATION (no exception raised) ***",
          file=sys.stderr)

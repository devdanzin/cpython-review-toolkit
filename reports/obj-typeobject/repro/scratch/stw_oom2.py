"""Control: same OOM sweep with an ABC that carries NO collection flags, so
_PyType_SetFlagsRecursive() is never called.  If the swallowed-MemoryError
window disappears, it is localized to set_flags_recursive()."""
import abc, sys, _testcapi
import collections.abc as cabc

SEQ = 1 << 5
mode = sys.argv[1]          # "seq" (hits SetFlagsRecursive) or "plain" (does not)
lo, hi = int(sys.argv[2]), int(sys.argv[3])
nkids = int(sys.argv[4])

class PlainABC(abc.ABC):
    pass

for start in range(lo, hi + 1):
    class Root:
        def __len__(self): return 0
        def __getitem__(self, i): raise IndexError
    kids = [type(f"K{i}", (Root,), {}) for i in range(nkids)]
    target = cabc.Sequence if mode == "seq" else PlainABC
    _testcapi.set_nomemory(start, start + 1)
    raised = "-"
    try:
        target.register(Root)
    except BaseException as e:
        raised = type(e).__name__
    leaked = "-"
    try:
        _testcapi.remove_mem_hooks()
    except SystemError as e:
        leaked = "PENDING-EXC-AFTER-register"
        _testcapi.remove_mem_hooks()
    nset = sum(bool(k.__flags__ & SEQ) for k in kids)
    print(f"{mode} n={nkids} start={start:3d} register_raised={raised:14s} "
          f"{leaked:28s} root={bool(Root.__flags__ & SEQ)!s:5s} kids={nset}/{nkids}",
          file=sys.stderr)

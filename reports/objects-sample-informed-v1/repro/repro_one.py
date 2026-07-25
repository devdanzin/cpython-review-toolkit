import sys, _testcapi
from typing import TypeVarTuple
Ts = TypeVarTuple('Ts')
alias = dict[str, tuple[*Ts]]
assert alias[int, str] is not None      # warm-up
n = int(sys.argv[1])
_testcapi.set_nomemory(n, n + 1)
try:
    print("RESULT:", alias[int, str])
except MemoryError:
    print("MemoryError (clean)")
_testcapi.remove_mem_hooks()

"""How many allocations does a payload really perform under the sweep harness?

Binary-search the largest index at which set_nomemory(n, n+1) still turns into
a MemoryError.  If the answer is huge for a tiny payload, the sweep's
'all indices are memory_error' result is measuring the harness, not the code
under test.
"""

import subprocess
import sys

PY = sys.argv[1]
SETUP = open(sys.argv[2]).read()
PAYLOAD = open(sys.argv[3]).read()

TEMPLATE = """\
import sys
import _testcapi
_NS = {{"__name__": "__main__"}}
_S = compile({setup!r}, "<s>", "exec")
_P = compile({payload!r}, "<p>", "exec")
exec(_S, _NS)
_testcapi.set_nomemory({n}, {n} + 1)
try:
    exec(_P, _NS)
except MemoryError:
    _testcapi.remove_mem_hooks()
    sys.exit(1)
except BaseException:
    _testcapi.remove_mem_hooks()
    sys.exit(2)
_testcapi.remove_mem_hooks()
sys.exit(0)
"""


def run(n: int) -> int:
    src = TEMPLATE.format(setup=SETUP, payload=PAYLOAD, n=n)
    p = subprocess.run([PY, "-c", src], capture_output=True, text=True)
    return p.returncode


lo, hi = 0, 1
while run(hi) == 1 and hi < 1 << 22:
    lo, hi = hi, hi * 2
print(f"first index that does NOT raise MemoryError is in ({lo}, {hi}]")
while hi - lo > 1:
    mid = (lo + hi) // 2
    if run(mid) == 1:
        lo = mid
    else:
        hi = mid
print(f"payload performs ~{hi} allocations (rc at {hi} = {run(hi)})")

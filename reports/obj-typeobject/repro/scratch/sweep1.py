"""Dense SINGLE-allocation-failure sweep: set_nomemory(n, n+1)."""
import subprocess, sys, os, textwrap, json

PY = sys.argv[1]
SETUP = sys.argv[2]
CODE = sys.argv[3]
MAXN = int(sys.argv[4])
WIDTH = int(sys.argv[5]) if len(sys.argv) > 5 else 1

TEMPLATE = """
import faulthandler, _testcapi, sys
faulthandler.enable()
ns = {{}}
exec({setup!r}, ns)
_testcapi.set_nomemory({a}, {b})
try:
    exec({code!r}, ns)
except MemoryError:
    pass
except Exception as e:
    pass
try:
    _testcapi.remove_mem_hooks()
except Exception:
    pass
sys.exit(0)
"""

crashes = []
for n in range(1, MAXN + 1):
    src = TEMPLATE.format(setup=SETUP, code=CODE, a=n, b=n + WIDTH)
    p = subprocess.run([PY, "-c", src], capture_output=True, timeout=90)
    rc = p.returncode
    if rc in (-11, 139, -6, 134):
        tail = p.stderr.decode("utf-8", "replace")
        crashes.append((n, rc, tail))
        print(f"n={n} rc={rc}")
        print(textwrap.indent(tail[:2500], "    "))
        if len(crashes) >= 6:
            break
if not crashes:
    print("no crash in 1..%d (width=%d)" % (MAXN, WIDTH))

"""Unarmed dry-run: prove setup AND payload both execute cleanly.

run_oom_sweep.py's child harness does not guard the setup phase, and a setup
that raises exits 1 -- the same code the harness uses for 'clean MemoryError'.
Every index of such a sweep is then reported as the SAFE outcome.  Always run
this first.
"""

import subprocess
import sys

PY, SETUP, PAYLOAD = sys.argv[1], sys.argv[2], sys.argv[3]
src = """
import sys
ns = {"__name__": "__main__"}
s = compile(open(%r).read(), "<setup>", "exec")
p = compile(open(%r).read(), "<payload>", "exec")
try:
    exec(s, ns)
except BaseException as e:
    import traceback; traceback.print_exc()
    print("SETUP FAILED:", type(e).__name__); sys.exit(3)
try:
    exec(p, ns)
except BaseException as e:
    import traceback; traceback.print_exc()
    print("PAYLOAD FAILED:", type(e).__name__); sys.exit(4)
print("DRY-RUN OK")
""" % (SETUP, PAYLOAD)
p = subprocess.run([PY, "-c", src], capture_output=True, text=True)
print(p.stdout.strip() or p.stderr.strip()[-1500:])
sys.exit(p.returncode)

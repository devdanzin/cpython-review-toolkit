"""P2-F2 re-verification: does a failed type-dict insertion destroy MemoryError?

Objects/typeobject.c:6714 converts EVERY _PyDict_SetItem_LockHeld failure into
    PyErr_Format(PyExc_AttributeError, "type object '%.50s' has no attribute '%U'")
with no PyErr_ExceptionMatches narrowing. A MemoryError from the dict insertion
is therefore replaced by a nonsensical AttributeError.

The control is the INSTANCE attribute path (object.__setattr__ -> the same
dict insertion, without the conversion), swept identically.

    python p2f2_probe.py <python-build> [max_n]

Prints, per index, the exception the two paths raise and its __context__.
"""

import subprocess
import sys

PY = sys.argv[1]
MAX_N = int(sys.argv[2]) if len(sys.argv) > 2 else 120

SETUP = open(
    "/home/danzin/projects/cpython-review-toolkit/reports/pass2/obj-typeobject"
    "/repro/oom/p2f2_setup.py"
).read()

TEMPLATE = """\
import sys
import _testcapi

_NS = {{"__name__": "__main__"}}
_S = compile({setup!r}, "<setup>", "exec")
_P = compile({payload!r}, "<payload>", "exec")
try:
    exec(_S, _NS)
except BaseException:
    import traceback
    traceback.print_exc()
    sys.exit(3)

_testcapi.set_nomemory({n}, {n} + 1)
try:
    exec(_P, _NS)
    _outcome = "no-exception"
except BaseException as e:
    _outcome = "%s: %s | __context__=%r" % (
        type(e).__name__, e, type(e.__context__).__name__ if e.__context__ else None)
finally:
    try:
        _testcapi.remove_mem_hooks()
    except Exception:
        pass
print(_outcome)
"""

TYPE_PAYLOAD = "for _nm in NAMES:\n    setattr(T, _nm, 1)\n"
INST_PAYLOAD = "for _nm in NAMES:\n    setattr(inst, _nm, 1)\n"


def probe(payload: str, n: int) -> str:
    src = TEMPLATE.format(setup=SETUP, payload=payload, n=n)
    p = subprocess.run(
        [PY, "-c", src], check=False, capture_output=True, text=True, timeout=60
    )
    if p.returncode != 0:
        return "rc=%d %s" % (p.returncode, p.stderr.strip().splitlines()[-1:] or "")
    return p.stdout.strip()


print(f"interpreter: {PY}")
print(f"{'n':>4}  {'TYPE path  (setattr on T)  ':<62}  INSTANCE path (setattr on inst)")
print("-" * 130)
divergent = []
for n in range(MAX_N):
    t = probe(TYPE_PAYLOAD, n)
    i = probe(INST_PAYLOAD, n)
    if t != "no-exception" or i != "no-exception":
        print(f"{n:>4}  {t:<62}  {i}")
    if t.startswith("AttributeError"):
        divergent.append((n, t, i))

print()
print(f"indices where the TYPE path raises AttributeError: {[d[0] for d in divergent]}")
for n, t, i in divergent:
    print(f"  n={n}: type -> {t}")
    print(f"        inst -> {i}")

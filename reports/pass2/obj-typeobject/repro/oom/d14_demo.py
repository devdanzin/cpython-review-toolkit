"""D-14 live demonstration: old harness vs fixed harness on a BROKEN setup.

Renders the pre-fix child template (setup exec outside any try) and the fixed
one, runs each against a real CPython build with a setup that raises, and prints
what run_oom_sweep.classify() makes of each exit code.

    python d14_demo.py <python-build>
"""

import subprocess
import sys

sys.path.insert(
    0,
    "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts",
)

import run_oom_sweep as R  # noqa: E402

PY = sys.argv[1]

# The exact shape that produced the false clean: a setup line that raises.
SETUP = "import pickle\nclass C: pass\npickle.dumps(lambda: 1)\n"
PAYLOAD = "class D(C): pass\n"

# Pre-fix child template, reconstructed verbatim from the unguarded version.
OLD_TEMPLATE = """\
import faulthandler
import sys

faulthandler.enable()

import _testcapi

_SETUP = {setup!r}
_PAYLOAD = {payload!r}
_NS = {{"__name__": "__main__"}}

_SETUP_CODE = compile(_SETUP, "<oom-setup>", "exec") if _SETUP else None
_PAYLOAD_CODE = compile(_PAYLOAD, "<oom-payload>", "exec")

if _SETUP_CODE is not None:
    exec(_SETUP_CODE, _NS)

_testcapi.set_nomemory({start}, {stop})
try:
    exec(_PAYLOAD_CODE, _NS)
except MemoryError:
    try:
        _testcapi.remove_mem_hooks()
    except Exception:
        pass
    sys.exit(1)
except BaseException:
    try:
        _testcapi.remove_mem_hooks()
    except Exception:
        pass
    sys.exit(2)
finally:
    try:
        _testcapi.remove_mem_hooks()
    except Exception:
        pass
sys.exit(0)
"""


def run(script: str) -> tuple[int, str]:
    p = subprocess.run(
        [PY, "-c", script], check=False, capture_output=True, text=True, timeout=60
    )
    return p.returncode, p.stderr


print(f"interpreter: {PY}")
print(f"setup (deliberately raising): {SETUP!r}\n")

old_counts: dict[str, int] = {}
for n in range(20):
    rc, err = run(OLD_TEMPLATE.format(setup=SETUP, payload=PAYLOAD, start=n, stop=n + 1))
    o = R.classify(rc, err)
    old_counts[o] = old_counts.get(o, 0) + 1
print(f"PRE-FIX  child, sweep 0..20 : {old_counts}")

new_counts: dict[str, int] = {}
for n in range(20):
    rc, err = run(R.build_child_script(PAYLOAD, n, setup=SETUP))
    o = R.classify(rc, err)
    new_counts[o] = new_counts.get(o, 0) + 1
print(f"FIXED    child, sweep 0..20 : {new_counts}")

d = R.dry_run(PY, PAYLOAD, setup=SETUP)
print(f"\nFIXED dry_run(): ok={d['ok']} phase={d['phase']} rc={d['returncode']}")
print("dry-run stderr tail:")
print("\n".join(d["stderr"].strip().splitlines()[-4:]))

res = R.sweep(PY, PAYLOAD, setup=SETUP, max_n=20)
print(f"\nFIXED sweep() top-level keys: {sorted(res)}")
print(f"sweep error: {res.get('error', '<none>')}")

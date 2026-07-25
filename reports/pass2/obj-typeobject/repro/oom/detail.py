"""Re-run specific sweep indices and print the HEAD of the child's stderr.

run_one keeps the stderr *tail* (that's where a faulthandler traceback lands),
which truncates the assertion line for long dumps. This re-runs the interesting
indices and shows the first lines instead.

    python detail.py <build> <setup.py> <payload.py> n [n ...]
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(
    0,
    "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts",
)

import run_oom_sweep as R  # noqa: E402

PY = str(Path("/home/danzin/projects/python_build_matrix/builds") / sys.argv[1] / "python")
setup = Path(sys.argv[2]).read_text()
payload = Path(sys.argv[3]).read_text()

for n in (int(a) for a in sys.argv[4:]):
    script = R.build_child_script(payload, n, setup=setup)
    p = subprocess.run(
        [PY, "-c", script], check=False, capture_output=True, text=True, timeout=120
    )
    outcome = R.classify(p.returncode, p.stderr)
    print(f"=== n={n}  rc={p.returncode}  outcome={outcome} ===")
    head = [
        line
        for line in p.stderr.strip().splitlines()
        if "Binary file" not in line and line.strip()
    ]
    print("\n".join(head[:8]))
    print()

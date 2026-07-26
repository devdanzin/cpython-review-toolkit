"""Build-matrix driver: one subprocess per (build, scenario) cell.

Usage:
    python3 ftrace_matrix_driver.py <script.py> <build>[,<build>...] [rounds] [probes]

Never runs two scenarios in one process: a crash must be attributable to a cell.
"""

import subprocess
import sys
from pathlib import Path

BUILDS = Path("/home/danzin/projects/python_build_matrix/builds")
TIMEOUT = 300


def scenarios(script: Path, interp: Path) -> list[str]:
    out = subprocess.run(
        [str(interp), str(script), "--list"], capture_output=True, text=True
    )
    return [ln.split()[0] for ln in out.stdout.splitlines() if ln.strip()]


def main() -> int:
    script = Path(sys.argv[1]).resolve()
    builds = sys.argv[2].split(",")
    rounds = sys.argv[3] if len(sys.argv) > 3 else "40000"
    probes = sys.argv[4] if len(sys.argv) > 4 else "7"

    first = BUILDS / builds[0] / "python"
    names = scenarios(script, first)
    print(f"{len(names)} scenarios x {len(builds)} builds")
    for build in builds:
        interp = BUILDS / build / "python"
        env = {"PYTHON_GIL": "0"} if "-ft-" in build else {}
        import os

        e = dict(os.environ)
        e.update(env)
        e.pop("PYTHONHOME", None)
        for name in names:
            try:
                p = subprocess.run(
                    [str(interp), str(script), name, rounds, probes],
                    capture_output=True,
                    timeout=TIMEOUT,
                    env=e,
                )
                rc = p.returncode
                # A crashing child can emit freed-heap bytes on stderr; never
                # let the driver die decoding them (AGENT_BRIEF non-UTF-8 rule).
                out = (p.stderr or p.stdout).decode("utf-8", "replace")
                tail = out.strip().splitlines()
                tail = tail[-1][:160] if tail else ""
            except subprocess.TimeoutExpired:
                rc, tail = 124, "TIMEOUT"
            flag = "" if rc == 0 else "   <<<<"
            print(f"{build:24s} {name:24s} rc={rc:<4d} {tail}{flag}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Run one repro scenario across a build matrix, one subprocess per cell.

Usage:
    python gil_matrix_driver.py <repro.py> <timeout_s> <builds,csv> <scenarios,csv> [extra args...]

Prints one line per cell: build scenario rc tail-of-output.
Never uses a heredoc; every child is a real .py file.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BUILDS = Path("/home/danzin/projects/python_build_matrix/builds")


def main() -> None:
    repro = sys.argv[1]
    timeout = float(sys.argv[2])
    builds = sys.argv[3].split(",")
    scenarios = sys.argv[4].split(",")
    extra = sys.argv[5:]

    for build in builds:
        exe = BUILDS / build / "python"
        if not exe.exists():
            print(f"{build:<26} -- MISSING BUILD")
            continue
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(Path.home()),
            "PYTHON_GIL": "0" if "-ft-" in build else "1",
        }
        for sc in scenarios:
            cmd = [str(exe), repro, sc, *extra]
            try:
                p = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
                )
                rc = p.returncode
                out = (p.stdout + p.stderr).strip().splitlines()
            except subprocess.TimeoutExpired as e:
                rc = 124
                raw = (e.stdout or b"") + (e.stderr or b"")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                out = raw.strip().splitlines()
            keep = [
                ln
                for ln in out
                if not ln.startswith("  Binary file")
                and not ln.startswith("  File ")
                and ln.strip()
            ]
            tail = " | ".join(keep[-4:])[:300]
            print(f"{build:<26} {sc:<18} rc={rc:<5} {tail}", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build-matrix driver for the lock-discipline repros.

One subprocess per (build, scenario) cell, hard timeout, PYTHON_GIL set per
build family.  A timeout is reported as TIMEOUT, not silently retried -- the
child's faulthandler has already dumped every thread's frames to stderr by
then, so the location is captured.

Usage:
  python lock_matrix_driver.py <script.py> <scenario>[,<scenario>...] [build,...]
"""

import os
import subprocess
import sys
from pathlib import Path

BUILDS = Path.home() / "projects/python_build_matrix/builds"
DEFAULT = ["debug-ft-nojit", "release-ft-nojit", "debug-gil-nojit", "release-gil-nojit"]
TIMEOUT = float(os.environ.get("LOI_TIMEOUT", "90"))


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    script = Path(sys.argv[1]).resolve()
    scenarios = sys.argv[2].split(",")
    builds = sys.argv[3].split(",") if len(sys.argv) > 3 else DEFAULT

    print(f"{'scenario':<28} {'build':<24} {'rc':>5}  result")
    print("-" * 78)
    for sc in scenarios:
        for b in builds:
            exe = BUILDS / b / "python"
            if not exe.exists():
                print(f"{sc:<28} {b:<24} {'--':>5}  MISSING")
                continue
            env = dict(os.environ)
            env["PYTHON_GIL"] = "0" if "-ft-" in b else "1"
            env.pop("PYTHONMALLOC", None)
            try:
                p = subprocess.run(
                    [str(exe), str(script), sc],
                    capture_output=True, text=True, timeout=TIMEOUT, env=env,
                )
                rc = p.returncode
                out = p.stdout + p.stderr
                res, iters, errs = "?", "?", []
                for line in p.stdout.splitlines():
                    if line.startswith("PROBE:result="):
                        res = line.split("=", 1)[1]
                    elif line.startswith("PROBE:iterations="):
                        iters = line.split("=", 1)[1]
                    elif line.startswith("PROBE:error="):
                        errs.append(line.split("=", 1)[1][:60])
                verdict = f"{res} iters={iters}"
                if errs:
                    verdict += " | " + "; ".join(errs[:2])
                if rc not in (0, 1):
                    verdict = f"CRASH rc={rc} " + verdict
                    tail = [x for x in out.splitlines() if x.strip()][-3:]
                    verdict += " || " + " / ".join(t[:70] for t in tail)
                print(f"{sc:<28} {b:<24} {rc:>5}  {verdict}")
            except subprocess.TimeoutExpired as e:
                so = (e.stdout or b"").decode(errors="replace")
                se = (e.stderr or b"").decode(errors="replace")
                tail = [x for x in (so + se).splitlines() if x.strip()][-6:]
                print(f"{sc:<28} {b:<24} {'TO':>5}  TIMEOUT@{TIMEOUT}s")
                for t in tail:
                    print(f"{'':<28} {'':<24} {'':>5}    | {t[:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

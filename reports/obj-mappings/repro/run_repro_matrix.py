"""Run one reproducer across a list of builds, N times each, and print a matrix.

Usage:
    python run_repro_matrix.py <script.py> <N> <build1[:extra args]> [build2 ...]

Each build is `~/projects/python_build_matrix/builds/<name>/python`.
Prints per-build: crashes/N, the distinct exit codes, and the first crash tail.
"""

import os
import subprocess
import sys

BUILDS = os.path.expanduser("~/projects/python_build_matrix/builds")
TIMEOUT = int(os.environ.get("REPRO_TIMEOUT", "600"))


def main():
    script = sys.argv[1]
    n = int(sys.argv[2])
    specs = sys.argv[3:]

    for spec in specs:
        parts = spec.split(":")
        build = parts[0]
        extra = parts[1].split() if len(parts) > 1 else []
        exe = os.path.join(BUILDS, build, "python")
        if not os.path.exists(exe):
            print("%-34s MISSING" % build, flush=True)
            continue
        env = dict(os.environ)
        if "-ft-" in build:
            env["PYTHON_GIL"] = "0"
        else:
            env.pop("PYTHON_GIL", None)
        env.setdefault("ASAN_OPTIONS", "detect_leaks=0:handle_abort=1:abort_on_error=0")
        env.setdefault("TSAN_OPTIONS", "halt_on_error=0:second_deadlock_stack=1")
        codes = []
        first_bad = None
        for i in range(n):
            try:
                p = subprocess.run([exe, script] + extra, capture_output=True,
                                   text=True, timeout=TIMEOUT, env=env,
                                   errors="replace")
                rc = p.returncode
                out = (p.stdout or "") + (p.stderr or "")
            except subprocess.TimeoutExpired as e:
                rc = "TIMEOUT"
                out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            codes.append(rc)
            if rc != 0 and first_bad is None:
                first_bad = out
        bad = sum(1 for c in codes if c != 0)
        print("%-34s %d/%d nonzero   codes=%s" % (build, bad, n, codes), flush=True)
        if first_bad:
            tail = "\n".join(first_bad.strip().splitlines()[-25:])
            print("      ---- first failure tail ----")
            for line in tail.splitlines():
                print("      " + line)
            print("      ----------------------------", flush=True)


if __name__ == "__main__":
    main()

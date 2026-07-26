"""Driver: run every io_postguard_null.py scenario on both builds + _pyio.

Prints a table of exit codes. Negative rc = killed by that signal
(-11 SIGSEGV, -6 SIGABRT). rc 0 = survived. rc 1 = clean Python exception.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "io_postguard_null.py")
MATRIX = os.path.expanduser("~/projects/python_build_matrix/builds")

SCENARIOS = [
    "detach_seeded",
    "close_after_detach",
    "truncate_after_detach",
    "readall_after_detach",
    "seek_after_closed_property",
]

BUILDS = ["debug-gil-nojit", "release-gil-nojit"]


def run(interp, scenario, backend):
    try:
        p = subprocess.run(
            [interp, PROBE, scenario, backend],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT", ""
    tail = (p.stdout + p.stderr).strip().splitlines()
    last = tail[-1][:88] if tail else ""
    return p.returncode, last


def main():
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    for scenario in SCENARIOS:
        print("=" * 78)
        print("SCENARIO:", scenario)
        for build in BUILDS:
            interp = os.path.join(MATRIX, build, "python")
            if not os.path.exists(interp):
                print(f"  {build:20s} MISSING")
                continue
            for backend in ("io", "_pyio"):
                codes = []
                last = ""
                for _ in range(reps):
                    rc, last = run(interp, scenario, backend)
                    codes.append(rc)
                crashes = sum(1 for c in codes if isinstance(c, int) and c < 0)
                print(
                    f"  {build:20s} {backend:6s} rc={codes[0]!s:>5} "
                    f"crash={crashes}/{len(codes)}  | {last}"
                )


if __name__ == "__main__":
    main()

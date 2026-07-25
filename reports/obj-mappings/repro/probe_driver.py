"""Run every case in cases.py in its own subprocess and report the exit code.

Usage:
    <interp> probe_driver.py <interp-under-test> [N] [case ...]

Exit codes:
    0    survived (or raised a clean Python exception that the case caught)
    1    clean Python exception (RecursionError / TypeError / ...)
    -11  SIGSEGV  <- native C-stack overflow, the class we are hunting
    -6   SIGABRT
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CASES_PY = os.path.join(HERE, "cases.py")


def main():
    interp = sys.argv[1]
    n = sys.argv[2] if len(sys.argv) > 2 else "200000"
    sys.path.insert(0, HERE)
    import cases  # noqa: E402

    wanted = sys.argv[3:] or sorted(cases.CASES)
    width = max(len(w) for w in wanted)
    for name in wanted:
        proc = subprocess.run(
            [interp, CASES_PY, name, n],
            capture_output=True, text=True, timeout=900,
        )
        rc = proc.returncode
        tag = {0: "ok", 1: "exc", -11: "SIGSEGV", -6: "SIGABRT"}.get(rc, f"rc={rc}")
        last_out = (proc.stdout.strip().splitlines() or [""])[-1]
        err = proc.stderr.strip().splitlines()
        last_err = err[-1] if err else ""
        print(f"{name:<{width}}  {tag:<8} out={last_out[:60]!r} err={last_err[:80]!r}",
              flush=True)


if __name__ == "__main__":
    main()
